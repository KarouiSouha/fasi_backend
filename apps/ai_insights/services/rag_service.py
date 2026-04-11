import logging

from django.conf import settings

from .direct_answer_service import DirectAnswerService
from .openai_service import OpenAIService
from .retrieval_service import RetrievalService

logger = logging.getLogger(__name__)


class RagService:
    def __init__(self):
        self.direct_answer = DirectAnswerService()
        self.openai = OpenAIService()
        self.retrieval = RetrievalService()
        self.max_response_tokens = getattr(settings, "AI_RAG_MAX_RESPONSE_TOKENS", 600)

    def run(self, question: str, company, context: str):
        direct = self.direct_answer.answer(question, company)
        if direct:
            return direct

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
            logger.warning("[RagService] OpenAI returned invalid response: %s", result)
            return None

        return result

    def _build_system_prompt(self) -> str:
        return (
            "You are WEEG Sales Intelligence Assistant. Use only the provided facts and the retrieved data. "
            "Answer with exact numbers for the question, and return valid JSON with keys: answer, decision_needed, decision_card, suggested_followups, urgency, topic. "
            "If no decision is needed set decision_card to null. "
            "Do not invent products, quantities, or LYD amounts. If a requested product or period is not found in the retrieved data, say that it is not present. "
            "If you cannot answer exactly from the data, say 'Je ne dispose pas d\'assez de données exactes dans les fichiers'."
        )

    def _build_prompt(self, question: str, business_context: str, retrieval: dict) -> str:
        parts = [
            f"Question:\n{question}",
            "Business context:\n" + (business_context or "No business context available."),
        ]

        if retrieval.get("mode") == "sql":
            parts.append("SQL summary:")
            parts.append(self._format_sql_context(retrieval))
        elif retrieval.get("mode") == "vector":
            parts.append("Semantic search results:")
            parts.append(self._format_vector_context(retrieval))
        else:
            parts.append("SQL summary:")
            parts.append(self._format_sql_context(retrieval.get("sql_context", {})))
            parts.append("Semantic search results:")
            parts.append(self._format_vector_context(retrieval.get("vector_context", {})))

        parts.append(
            "Return an answer in French if the user asks in French, otherwise return English. "
            "Keep the answer concise and data-driven."
        )
        return "\n\n".join(parts)

    def _format_sql_context(self, sql_context):
        if not sql_context:
            return "No SQL data found."

        if sql_context.get("product_sales"):
            return (
                f"Product: {sql_context['product_sales']['product_name']}\n"
                f"Quantity sold: {sql_context['product_sales']['total_qty']}\n"
                f"Turnover: {sql_context['product_sales']['total_revenue']} LYD\n"
                f"Transactions: {sql_context['product_sales']['transactions']}\n"
                f"Period: {sql_context['product_sales']['start_date']} to {sql_context['product_sales']['end_date']}"
            )

        if sql_context.get("product_sales_list"):
            lines = [
                f"Period: {sql_context['summary']['start_date']} to {sql_context['summary']['end_date']}",
                f"Total quantity: {sql_context['summary']['total_qty']}",
                f"Total turnover: {sql_context['summary']['total_revenue']} LYD",
                f"Transactions: {sql_context['summary']['transactions']}",
                "Product-level details:",
            ]
            for item in sql_context["product_sales_list"]:
                lines.append(
                    f"  - {item['product_name']}: qty={item['total_qty']}, turnover={item['total_revenue']} LYD, txns={item['transactions']}"
                )
            return "\n".join(lines)

        summary = sql_context.get("summary") or {}
        lines = [
            f"Period: {summary.get('start_date')} to {summary.get('end_date')}",
            f"Total quantity: {summary.get('total_qty')}",
            f"Total turnover: {summary.get('total_revenue')} LYD",
            f"Transactions: {summary.get('transactions')}",
        ]
        if sql_context.get("top_products"):
            lines.append("Top products:")
            for product in sql_context["top_products"]:
                lines.append(
                    f"  - {product['material_name']}: {product['total_qty']} units, {product['total_revenue']} LYD"
                )
        return "\n".join(lines)

    def _format_vector_context(self, vector_context):
        items = vector_context.get("items") or []
        if not items:
            return "No semantic results found."

        lines = []
        for item in items[:5]:
            snippet = item.get("text", "").replace("\n", " ")[:300]
            lines.append(f"- {snippet} (score={item.get('score'):.3f})")
        return "\n".join(lines)
