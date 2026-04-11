"""
apps/ai_insights/services/rag_service.py
-----------------------------------------
Orchestration du pipeline RAG.

Flux :
  1. DirectAnswerService → toutes les questions de données (produits, revenue, dates)
                           Retourne toujours une réponse — jamais d'hallucination.
  2. SQL fallback        → questions de données sans pattern reconnu
  3. LLM (OpenAI)        → uniquement pour analyse, conseils, explications

Règle absolue : le LLM ne répond JAMAIS à une question de données brutes.
"""

import logging

from django.conf import settings

from .direct_answer_service import DirectAnswerService
from .openai_service import OpenAIService
from .retrieval_service import RetrievalService

logger = logging.getLogger(__name__)

DATA_QUERY_KEYWORDS = [
    # Anglais
    "top-selling", "top selling", "best-selling", "best selling",
    "most sold", "best seller", "top product", "top products",
    "total sales", "total revenue", "total turnover",
    "sales data", "sales for", "sold in", "revenue in",
    "how much", "how many", "units sold", "turnover",
    # Français
    "produit le plus", "meilleur produit", "ventes totales",
    "chiffre affaires", "combien",
]


class RagService:
    def __init__(self):
        self.direct_answer = DirectAnswerService()
        self.openai = OpenAIService()
        self.retrieval = RetrievalService()
        self.max_response_tokens = getattr(
            settings, "AI_RAG_MAX_RESPONSE_TOKENS", 600
        )

    def run(self, question: str, company, context: str, companies: list = None):
        """
        Args:
            question  : question de l'utilisateur
            company   : société principale
            context   : contexte business (caches analyzers)
            companies : toutes les sociétés autorisées (multi-société)
        """
        if not companies:
            companies = [company] if company else []

        # ── Étape 1 : DirectAnswerService (SQL direct) ────────────────────────
        direct = self.direct_answer.answer(question, company, companies=companies)
        if direct:
            logger.debug("[RagService] Direct answer: %s", question[:80])
            return direct

        # ── Étape 2 : Question de données sans pattern → SQL fallback ─────────
        if self._is_data_query(question):
            logger.debug("[RagService] SQL fallback for: %s", question[:80])
            return self._sql_fallback(question, company, companies)

        # ── Étape 3 : Question analytique → LLM avec contexte ─────────────────
        mode = self.retrieval.get_query_mode(question)
        if mode == "hybrid":
            retrieval = self.retrieval.build_hybrid_context(question, company)
        elif mode == "vector":
            retrieval = self.retrieval.build_vector_context(question, company)
        else:
            retrieval = self.retrieval.build_sql_context(question, company)

        user_prompt = self._build_prompt(question, context, retrieval)
        result = self.openai.complete(
            system_prompt=self._build_system_prompt(),
            user_prompt=user_prompt,
            analyzer=f"rag_{mode}",
            max_tokens=self.max_response_tokens,
        )

        if not result or result.get("error"):
            logger.warning("[RagService] OpenAI invalid response: %s", result)
            return None

        return result

    # ── SQL fallback ──────────────────────────────────────────────────────────

    def _sql_fallback(self, question: str, company, companies: list) -> dict:
        from datetime import date
        from .query_weaver_service import QueryWeaverService
        from .sql_service import SQLService

        qw  = QueryWeaverService()
        sql = SQLService()

        start_date, end_date = qw.parse_date_range(question)
        if not start_date:
            start_date = sql.get_earliest_transaction_date(company)
            end_date   = date.today()

        is_group = len(companies) > 1 and any(
            kw in question.lower() for kw in [
                "group", "groupe", "all", "toutes", "global", "combined"
            ]
        )

        if is_group:
            summaries = sql.get_sales_summary_by_company(
                companies, start_date, end_date
            )
            total_rev = sum(s["total_revenue"] for s in summaries)
            top = sql.get_top_sold_products(
                company=None,
                start_date=start_date,
                end_date=end_date,
                top_n=3,
                companies=companies,
            )
        else:
            summary = sql.get_sales_summary(company, start_date, end_date)
            summaries = [summary]
            total_rev = summary["total_revenue"]
            top = sql.get_top_sold_products(company, start_date, end_date, top_n=3)

        period = f"from {start_date.isoformat()} to {end_date.isoformat()}"

        if total_rev == 0:
            answer = (
                f"No sales data found {period}. "
                f"Please ensure transactions have been imported for this period."
            )
        else:
            top_str = ""
            if top:
                top_str = " Top products: " + "; ".join(
                    f"{p['material_name']} "
                    f"({p.get('company_name', '')} — "
                    f"{int(p['total_qty'] or 0):,} units, "
                    f"{float(p['total_revenue'] or 0):,.2f} LYD)"
                    for p in top
                ) + "."

            if is_group:
                total_qty = sum(s["total_qty"] for s in summaries)
                total_txn = sum(s["transactions"] for s in summaries)
                company_lines = " | ".join(
                    f"{s['company_name']}: {float(s['total_revenue']):,.2f} LYD"
                    for s in summaries
                )
                answer = (
                    f"Group sales {period}: "
                    f"{int(total_qty):,} units, "
                    f"{total_rev:,.2f} LYD total. "
                    f"By company: {company_lines}.{top_str}"
                )
            else:
                s = summaries[0]
                answer = (
                    f"Sales {period}: "
                    f"{int(s.get('total_qty') or 0):,} units, "
                    f"{float(s.get('total_revenue') or 0):,.2f} LYD, "
                    f"{s.get('transactions') or 0:,} transactions."
                    f"{top_str}"
                )

        return {
            "answer": answer,
            "decision_needed": False,
            "decision_card": None,
            "suggested_followups": [
                "Do you want a breakdown by product?",
                "Do you want to compare with another period?",
                "Which customers drove the most revenue?",
            ],
            "urgency": "medium",
            "topic": "revenue",
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_data_query(question: str) -> bool:
        q = (question or "").lower()
        return any(kw in q for kw in DATA_QUERY_KEYWORDS)

    def _build_system_prompt(self) -> str:
        return (
            "You are WEEG Sales Intelligence Assistant. "
            "Use ONLY the provided SQL data and business context to answer. "
            "Return valid JSON with keys: answer, decision_needed, decision_card, "
            "suggested_followups, urgency, topic. "
            "Set decision_card to null if no decision is needed. "
            "NEVER invent products, quantities, or LYD amounts. "
            "NEVER claim that data for a year does not exist — "
            "if the SQL context shows no data, say exactly that."
        )

    def _build_prompt(self, question: str, business_context: str, retrieval: dict) -> str:
        parts = [
            f"Question:\n{question}",
            "Business context:\n" + (business_context or "No context available."),
        ]
        mode = retrieval.get("mode", "sql")
        if mode == "sql":
            parts.append("SQL data:\n" + self._format_sql_context(retrieval))
        elif mode == "vector":
            parts.append("Semantic results:\n" + self._format_vector_context(retrieval))
        else:
            parts.append("SQL data:\n" + self._format_sql_context(
                retrieval.get("sql_context", {})))
            parts.append("Semantic results:\n" + self._format_vector_context(
                retrieval.get("vector_context", {})))

        parts.append(
            "Answer in French if asked in French, otherwise English. "
            "Be concise and data-driven."
        )
        return "\n\n".join(parts)

    def _format_sql_context(self, sql_context: dict) -> str:
        if not sql_context:
            return "No SQL data found."
        if sql_context.get("product_sales"):
            ps = sql_context["product_sales"]
            return (
                f"Product: {ps['product_name']}\n"
                f"Quantity sold: {ps['total_qty']}\n"
                f"Turnover: {ps['total_revenue']} LYD\n"
                f"Transactions: {ps['transactions']}\n"
                f"Period: {ps['start_date']} to {ps['end_date']}"
            )
        if sql_context.get("product_sales_list"):
            s = sql_context.get("summary", {})
            lines = [
                f"Period: {s.get('start_date')} to {s.get('end_date')}",
                f"Total quantity: {s.get('total_qty')}",
                f"Total turnover: {s.get('total_revenue')} LYD",
                "Products:",
            ]
            for item in sql_context["product_sales_list"]:
                lines.append(
                    f"  - {item['product_name']}: "
                    f"qty={item['total_qty']}, "
                    f"{item['total_revenue']} LYD"
                )
            return "\n".join(lines)
        summary = sql_context.get("summary") or {}
        lines = [
            f"Period: {summary.get('start_date')} to {summary.get('end_date')}",
            f"Total qty: {summary.get('total_qty')}",
            f"Total revenue: {summary.get('total_revenue')} LYD",
            f"Transactions: {summary.get('transactions')}",
        ]
        for p in sql_context.get("top_products", []):
            lines.append(
                f"  - {p['material_name']}: "
                f"{p['total_qty']} units, {p['total_revenue']} LYD"
            )
        return "\n".join(lines)

    def _format_vector_context(self, vector_context: dict) -> str:
        items = vector_context.get("items") or []
        if not items:
            return "No semantic results found."
        return "\n".join(
            f"- {item.get('text', '')[:300]} (score={item.get('score', 0):.3f})"
            for item in items[:5]
        )