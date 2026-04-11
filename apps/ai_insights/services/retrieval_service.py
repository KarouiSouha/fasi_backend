"""
apps/ai_insights/services/retrieval_service.py
------------------------------------------------
- Qdrant silencieux si QDRANT_URL non configuré (optionnel)
- Warning uniquement si URL configurée mais connexion échoue
- Mode SQL par défaut si Qdrant indisponible
"""

import logging

from .langgraph_workflow import LangGraphWorkflow
from .query_weaver_service import QueryWeaverService
from .sql_service import SQLService
from .qdrant_service import QdrantService, QdrantServiceUnavailable

logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self):
        self.workflow       = LangGraphWorkflow()
        self.query_weaver   = QueryWeaverService()
        self.sql_service    = SQLService()
        self.qdrant_service = None
        self._init_qdrant()

    def _init_qdrant(self):
        """
        Initialise Qdrant seulement si QDRANT_URL est configuré.
        - Pas d'URL → silencieux (Qdrant est optionnel)
        - URL configurée mais inaccessible → warning (l'opérateur doit le savoir)
        """
        from django.conf import settings
        qdrant_url = getattr(settings, "QDRANT_URL", "").strip()

        if not qdrant_url:
            # Non configuré intentionnellement — aucun log
            return

        try:
            self.qdrant_service = QdrantService()
            logger.info("[RetrievalService] Qdrant connected: %s", qdrant_url)
        except QdrantServiceUnavailable as exc:
            logger.warning(
                "[RetrievalService] Qdrant configured at %s but unavailable: %s",
                qdrant_url, exc,
            )

    def get_query_mode(self, question: str) -> str:
        # Sans Qdrant → toujours SQL
        if not self.qdrant_service:
            return "sql"
        return self.workflow.decide(question)["mode"]

    def build_sql_context(self, question: str, company):
        start_date, end_date = self.query_weaver.parse_date_range(question)
        if not start_date or not end_date:
            import datetime
            today      = datetime.date.today()
            start_date = today.replace(day=1)
            end_date   = today

        product_names = self.query_weaver.parse_product_names(question, company)
        product_name  = self.query_weaver.parse_product_name(question, company)

        if len(product_names) > 1:
            return {
                "mode": "sql",
                "summary": self.sql_service.get_sales_summary(
                    company, start_date, end_date
                ),
                "product_sales_list": [
                    self.sql_service.get_product_sales(
                        company, name, start_date, end_date
                    )
                    for name in product_names
                ],
                "product_name": product_names,
            }

        if "most" in question.lower() and "sold" in question.lower():
            return {
                "mode": "sql",
                "summary": self.sql_service.get_sales_summary(
                    company, start_date, end_date
                ),
                "top_products": self.sql_service.get_top_sold_products(
                    company, start_date, end_date, top_n=5
                ),
                "product_name": product_name,
            }

        if product_name:
            return {
                "mode": "sql",
                "product_sales": self.sql_service.get_product_sales(
                    company, product_name, start_date, end_date
                ),
            }

        return {
            "mode": "sql",
            "summary": self.sql_service.get_sales_summary(
                company, start_date, end_date
            ),
        }

    def build_vector_context(self, question: str, company):
        if not self.qdrant_service:
            return {"mode": "vector", "items": []}

        from .openai_service import OpenAIService
        openai          = OpenAIService()
        query_embedding = openai.embed_texts([question])[0]
        hits            = self.qdrant_service.search(
            query_embedding, company_id=str(company.id), top=5
        )
        items = []
        for hit in hits:
            payload = getattr(hit, "payload", None) or {}
            items.append({
                "id":       str(getattr(hit, "id", "")),
                "score":    float(getattr(hit, "score", 0) or 0),
                "text":     payload.get("text") or payload.get("content") or "",
                "metadata": payload,
            })
        return {"mode": "vector", "items": items}

    def build_hybrid_context(self, question: str, company):
        return {
            "mode":           "hybrid",
            "sql_context":    self.build_sql_context(question, company),
            "vector_context": self.build_vector_context(question, company),
        }