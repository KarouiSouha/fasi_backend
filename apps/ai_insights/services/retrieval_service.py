import logging

from .langgraph_workflow import LangGraphWorkflow
from .query_weaver_service import QueryWeaverService
from .sql_service import SQLService
from .qdrant_service import QdrantService, QdrantServiceUnavailable

logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self):
        self.workflow = LangGraphWorkflow()
        self.query_weaver = QueryWeaverService()
        self.sql_service = SQLService()
        self.qdrant_service = None
        try:
            self.qdrant_service = QdrantService()
        except QdrantServiceUnavailable as exc:
            logger.warning("[RetrievalService] Qdrant unavailable: %s", exc)

    def get_query_mode(self, question: str) -> str:
        return self.workflow.decide(question)["mode"]

    def build_sql_context(self, question: str, company):
        start_date, end_date = self.query_weaver.parse_date_range(question)
        if not start_date or not end_date:
            today = __import__("datetime").date.today()
            start_date = today.replace(day=1)
            end_date = today

        product_name = self.query_weaver.parse_product_name(question, company)
        product_names = self.query_weaver.parse_product_names(question, company)
        if len(product_names) > 1:
            product_sales_list = [
                self.sql_service.get_product_sales(company, name, start_date, end_date)
                for name in product_names
            ]
            return {
                "mode": "sql",
                "summary": self.sql_service.get_sales_summary(company, start_date, end_date),
                "product_sales_list": product_sales_list,
                "product_name": product_names,
            }

        if "most" in question.lower() and "sold" in question.lower():
            top_products = self.sql_service.get_top_sold_products(company, start_date, end_date, top_n=5)
            return {
                "mode": "sql",
                "summary": self.sql_service.get_sales_summary(company, start_date, end_date),
                "top_products": top_products,
                "product_name": product_name,
            }

        if product_name:
            return {
                "mode": "sql",
                "product_sales": self.sql_service.get_product_sales(company, product_name, start_date, end_date),
            }

        return {
            "mode": "sql",
            "summary": self.sql_service.get_sales_summary(company, start_date, end_date),
        }

    def build_vector_context(self, question: str, company):
        if not self.qdrant_service:
            return {"mode": "vector", "items": []}

        from .openai_service import OpenAIService

        openai = OpenAIService()
        query_embedding = openai.embed_texts([question])[0]
        hits = self.qdrant_service.search(query_embedding, company_id=str(company.id), top=5)

        items = []
        for hit in hits:
            payload = getattr(hit, "payload", None) or {}
            items.append({
                "id": str(getattr(hit, "id", "")),
                "score": float(getattr(hit, "score", 0) or 0),
                "text": payload.get("text") or payload.get("content") or "",
                "metadata": payload,
            })

        return {"mode": "vector", "items": items}

    def build_hybrid_context(self, question: str, company):
        sql_context = self.build_sql_context(question, company)
        vector_context = self.build_vector_context(question, company)
        return {
            "mode": "hybrid",
            "sql_context": sql_context,
            "vector_context": vector_context,
        }
