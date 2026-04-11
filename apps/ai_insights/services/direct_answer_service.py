import logging
from datetime import date

from .query_weaver_service import QueryWeaverService
from .sql_service import SQLService

logger = logging.getLogger(__name__)


class DirectAnswerService:
    def __init__(self):
        self.query_weaver = QueryWeaverService()
        self.sql_service = SQLService()

    def answer(self, question: str, company):
        text = (question or "").strip()
        if not text:
            return None

        start_date, end_date = self.query_weaver.parse_date_range(text)
        explicit_dates = bool(start_date and end_date)
        if not explicit_dates:
            # If the user did not specify a period, use earliest transaction date from DB
            start_date = self.sql_service.get_earliest_transaction_date(company)
            end_date = date.today()

        # Priority 1: Try to find specific products first (highest priority)
        product_names = self.query_weaver.parse_product_names(text, company)
        if product_names:
            product_sales = [
                self.sql_service.get_product_sales(company, name, start_date, end_date)
                for name in product_names
            ]
            if any(item['total_qty'] or item['total_revenue'] for item in product_sales):
                answer = self._build_product_sales_answer(product_sales, start_date, end_date)
                return self._build_response(answer)
            # Product found but no sales data => return no-data
            return self._no_data_response()

        # Priority 2: Check for top-selling products question
        if self._is_top_product_question(text):
            top_products = self.sql_service.get_top_sold_products(company, start_date, end_date, top_n=1)
            if not top_products:
                return self._no_data_response()
            top = top_products[0]
            answer = (
                f"In {start_date.strftime('%B %Y')}, the top-selling product was '{top['material_name']}', "
                f"with {int(top['total_qty'])} units sold and a turnover of {top['total_revenue']:.2f} LYD."
            )
            return self._build_response(answer)

        # Priority 3: Check for total sales question (but be strict - only clear total/summary questions)
        if self._is_total_sales_question(text):
            summary = self.sql_service.get_sales_summary(company, start_date, end_date)
            if not summary['total_qty'] and not summary['total_revenue']:
                return self._no_data_response()
            answer = (
                f"From {start_date.isoformat()} to {end_date.isoformat()}, total sales were "
                f"{summary['total_qty']} units for {summary['total_revenue']:.2f} LYD across {summary['transactions']} transactions."
            )
            return self._build_response(answer)

        # Priority 4: No products matched, no specific question detected => let LLM handle it
        return None

    def _build_product_sales_answer(self, product_sales, start_date, end_date):
        if len(product_sales) == 1:
            item = product_sales[0]
            return (
                f"Between {start_date.isoformat()} and {end_date.isoformat()}, '{item['product_name']}' sold "
                f"{int(item['total_qty'])} units for a total of {item['total_revenue']:.2f} LYD."
            )

        lines = [
            f"Between {start_date.isoformat()} and {end_date.isoformat()}, sales by product were:"
        ]
        for item in product_sales:
            lines.append(
                f"- {item['product_name']}: {int(item['total_qty'])} units, {item['total_revenue']:.2f} LYD"
            )
        return " ".join(lines)

    def _build_response(self, answer: str):
        return {
            "answer": answer,
            "decision_needed": False,
            "decision_card": None,
            "suggested_followups": [
                "Should we compare this to the previous month?",
                "Do you want the sales broken down by customer or branch?",
            ],
            "urgency": "medium",
            "topic": "revenue",
        }

    def _no_data_response(self):
        return {
            "answer": "Je ne dispose pas d'assez de données exactes dans les fichiers pour répondre précisément à cette question.",
            "decision_needed": False,
            "decision_card": None,
            "suggested_followups": [
                "Voulez-vous une autre période ?",
                "Voulez-vous vérifier un autre produit ?",
            ],
            "urgency": "low",
            "topic": "revenue",
        }

    def _normalize_text(self, text: str) -> str:
        return (text or "").lower().strip()

    def _is_top_product_question(self, text: str) -> bool:
        normalized = self._normalize_text(text)
        return any(token in normalized for token in [
            "hot sales", "hot product", "top-selling", "top selling", "best-selling", "best selling",
            "most sold", "best seller", "meilleur produit", "produit le plus vendu", "produit chaud"
        ])

    def _is_total_sales_question(self, text: str) -> bool:
        normalized = self._normalize_text(text)
        
        # Exclude questions that mention specific products or items
        excludes = ["product", "produit", "item", "article", "camera", "câmera", "specific"]
        if any(exclude in normalized for exclude in excludes):
            return False
        
        # Only match clear total/summary questions
        return any(token in normalized for token in [
            "total sales", "total turnover", "total revenue", "overall sales", "sales summary",
            "chiffre affaires", "ca total", "ventes totales", "ventes globales", "résumé", "summary"
        ])
