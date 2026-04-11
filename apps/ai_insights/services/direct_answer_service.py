"""
apps/ai_insights/services/direct_answer_service.py
----------------------------------------------------
Répond aux questions de données directement depuis la DB — jamais via le LLM.

Supporte :
  - Société unique  → company
  - Multi-sociétés  → companies=[...]
  - Questions groupe (total groupe, comparaison entre sociétés)

Règle : si la question est clairement sur des données (revenue, produits, dates),
ce service retourne TOUJOURS une réponse. Il ne retourne None que pour les
questions analytiques/conseils (pourquoi, comment, tendance...).
"""

import re
import logging
from datetime import date

from .query_weaver_service import QueryWeaverService
from .sql_service import SQLService

logger = logging.getLogger(__name__)


class DirectAnswerService:
    def __init__(self):
        self.query_weaver = QueryWeaverService()
        self.sql_service = SQLService()

    def answer(self, question: str, company, companies: list = None):
        """
        Args:
            question  : question de l'utilisateur
            company   : société principale de l'utilisateur
            companies : toutes les sociétés autorisées (pour questions groupe)
        """
        text = (question or "").strip()
        if not text:
            return None

        # companies par défaut = [company] si non fourni
        if not companies:
            companies = [company] if company else []

        # ── Parse dates ───────────────────────────────────────────────────────
        start_date, end_date = self.query_weaver.parse_date_range(text)
        explicit_dates = bool(start_date and end_date)
        if not explicit_dates:
            start_date = self.sql_service.get_earliest_transaction_date(company)
            end_date = date.today()

        is_group = self._is_group_question(text)
        target_companies = companies if is_group else [company]

        # ── Priorité 1 : produit spécifique ──────────────────────────────────
        product_names = self.query_weaver.parse_product_names(text, company)
        if product_names:
            if is_group and len(companies) > 1:
                all_sales = []
                for pname in product_names:
                    rows = self.sql_service.get_product_sales_all_companies(
                        companies, pname, start_date, end_date
                    )
                    all_sales.extend(rows)
                answer = self._build_multi_company_product_answer(
                    all_sales, start_date, end_date, text
                )
            else:
                product_sales = [
                    self.sql_service.get_product_sales(
                        company, pname, start_date, end_date
                    )
                    for pname in product_names
                ]
                has_data = any(
                    (s.get("total_qty") or 0) > 0 or (s.get("total_revenue") or 0) > 0
                    for s in product_sales
                )
                if has_data:
                    answer = self._build_product_sales_answer(
                        product_sales, start_date, end_date, text
                    )
                else:
                    period = self._period_label(start_date, end_date, text)
                    names_str = ", ".join(f"'{n}'" for n in product_names)
                    answer = (
                        f"No sales found for {names_str} {period.lower()}. "
                        f"The product exists in the database but had no "
                        f"recorded sales in this period."
                    )
            return self._build_response(answer)

        # ── Priorité 2 : top-selling ──────────────────────────────────────────
        if self._is_top_product_question(text):
            top_n = self._extract_top_n(text)
            if is_group and len(companies) > 1:
                top = self.sql_service.get_top_sold_products(
                    company=None,
                    start_date=start_date,
                    end_date=end_date,
                    top_n=top_n,
                    companies=companies,
                )
            else:
                top = self.sql_service.get_top_sold_products(
                    company=company,
                    start_date=start_date,
                    end_date=end_date,
                    top_n=top_n,
                )
            if top:
                answer = self._build_top_products_answer(
                    top, start_date, end_date, text
                )
            else:
                period = self._period_label(start_date, end_date, text)
                answer = (
                    f"No sales data found {period.lower()}. "
                    f"Please verify that transactions have been imported "
                    f"for this period."
                )
            return self._build_response(answer)

        # ── Priorité 3 : total sales ──────────────────────────────────────────
        if self._is_total_sales_question(text):
            period = self._period_label(start_date, end_date, text)
            if is_group and len(companies) > 1:
                summaries = self.sql_service.get_sales_summary_by_company(
                    companies, start_date, end_date
                )
                answer = self._build_group_summary_answer(
                    summaries, start_date, end_date, text
                )
            else:
                summary = self.sql_service.get_sales_summary(
                    company, start_date, end_date
                )
                if (summary.get("total_qty") or 0) > 0 or \
                   (summary.get("total_revenue") or 0) > 0:
                    answer = (
                        f"{period}, total sales: "
                        f"{int(summary['total_qty'] or 0):,} units, "
                        f"{float(summary['total_revenue'] or 0):,.2f} LYD, "
                        f"{summary['transactions'] or 0:,} transactions."
                    )
                else:
                    answer = (
                        f"No sales transactions found {period.lower()}. "
                        f"Please verify that data has been imported "
                        f"for this period."
                    )
            return self._build_response(answer)

        # ── Pas une question de données → LLM ────────────────────────────────
        return None

    # ── Builders de réponses ──────────────────────────────────────────────────

    def _build_top_products_answer(
        self, top: list, start_date: date, end_date: date, text: str
    ) -> str:
        period = self._period_label(start_date, end_date, text)
        multi_company = len(set(p.get("company_name", "") for p in top)) > 1

        if len(top) == 1:
            p = top[0]
            company_part = f" ({p['company_name']})" if multi_company else ""
            return (
                f"{period}, the top-selling product was "
                f"'{p['material_name']}'{company_part}, "
                f"with {int(p['total_qty'] or 0):,} units sold "
                f"and {float(p['total_revenue'] or 0):,.2f} LYD revenue."
            )

        lines = [f"{period}, top {len(top)} products:"]
        for rank, p in enumerate(top, 1):
            company_part = f" [{p['company_name']}]" if multi_company else ""
            lines.append(
                f"  {rank}. {p['material_name']}{company_part} — "
                f"{int(p['total_qty'] or 0):,} units, "
                f"{float(p['total_revenue'] or 0):,.2f} LYD"
            )
        return "\n".join(lines)

    def _build_product_sales_answer(
        self, product_sales: list, start_date: date, end_date: date, text: str
    ) -> str:
        period = self._period_label(start_date, end_date, text)
        if len(product_sales) == 1:
            item = product_sales[0]
            return (
                f"{period}, '{item['product_name']}' "
                f"({item.get('company_name', '')}) — "
                f"{int(item['total_qty'] or 0):,} units sold, "
                f"{float(item['total_revenue'] or 0):,.2f} LYD, "
                f"{item.get('transactions', 0):,} transactions."
            )
        lines = [f"{period}, sales by product:"]
        for item in product_sales:
            lines.append(
                f"  · {item['product_name']} "
                f"({item.get('company_name', '')}): "
                f"{int(item['total_qty'] or 0):,} units, "
                f"{float(item['total_revenue'] or 0):,.2f} LYD"
            )
        return "\n".join(lines)

    def _build_multi_company_product_answer(
        self, all_sales: list, start_date: date, end_date: date, text: str
    ) -> str:
        period = self._period_label(start_date, end_date, text)
        if not all_sales:
            return (
                f"No sales found {period.lower()} "
                f"across all companies for this product."
            )
        lines = [f"{period}, sales by company:"]
        for s in all_sales:
            lines.append(
                f"  · {s.get('company_name', '?')} — "
                f"'{s['product_name']}': "
                f"{int(s['total_qty'] or 0):,} units, "
                f"{float(s['total_revenue'] or 0):,.2f} LYD"
            )
        return "\n".join(lines)

    def _build_group_summary_answer(
        self, summaries: list, start_date: date, end_date: date, text: str
    ) -> str:
        period = self._period_label(start_date, end_date, text)
        total_rev = sum(s["total_revenue"] for s in summaries)
        total_qty = sum(s["total_qty"] for s in summaries)
        total_txn = sum(s["transactions"] for s in summaries)

        lines = [
            f"{period}, group total: "
            f"{int(total_qty):,} units, "
            f"{total_rev:,.2f} LYD, "
            f"{total_txn:,} transactions.",
            "Breakdown by company:",
        ]
        for s in summaries:
            lines.append(
                f"  · {s['company_name']}: "
                f"{float(s['total_revenue']):,.2f} LYD "
                f"({s['transactions']:,} transactions)"
            )
        return "\n".join(lines)

    # ── Period label ──────────────────────────────────────────────────────────

    MONTH_DISPLAY = {
        "january": "January", "janvier": "January",
        "february": "February", "février": "February", "fevrier": "February",
        "march": "March", "mars": "March",
        "april": "April", "avril": "April",
        "may": "May", "mai": "May",
        "june": "June", "juin": "June",
        "july": "July", "juillet": "July",
        "august": "August", "août": "August", "aout": "August",
        "september": "September", "septembre": "September",
        "october": "October", "octobre": "October",
        "november": "November", "novembre": "November",
        "december": "December", "décembre": "December", "decembre": "December",
    }

    def _period_label(self, start_date: date, end_date: date, text: str) -> str:
        t = (text or "").lower()
        year_m = re.search(r"\b(20\d{2})\b", t)
        month_m = None
        for token, display in self.MONTH_DISPLAY.items():
            if token in t:
                month_m = display
                break

        if month_m and year_m:
            return f"In {month_m} {year_m.group(1)}"
        if month_m:
            return f"In {month_m} {start_date.year}"
        if year_m:
            return f"In {year_m.group(1)}"
        if "this month" in t or "ce mois" in t:
            return f"In {start_date.strftime('%B %Y')}"
        if "ytd" in t or "year to date" in t:
            return f"Year-to-date {start_date.year}"
        return f"From {start_date.isoformat()} to {end_date.isoformat()}"

    # ── Détecteurs ────────────────────────────────────────────────────────────

    @staticmethod
    def _is_group_question(text: str) -> bool:
        t = (text or "").lower()
        return any(kw in t for kw in [
            "all companies", "toutes les sociétés", "toutes les societes",
            "group", "groupe", "overall", "global",
            "all branches", "toutes les branches",
            "across", "combined", "total group", "total groupe",
            "ensemble", "consolidé", "consolide",
        ])

    @staticmethod
    def _is_top_product_question(text: str) -> bool:
        t = (text or "").lower()
        return any(kw in t for kw in [
            "top-selling", "top selling", "best-selling", "best selling",
            "most sold", "best seller", "top product", "top products",
            "best product", "best products", "highest selling",
            "most popular", "meilleur produit", "produit le plus vendu",
            "hot product", "hot sales",
        ])

    @staticmethod
    def _is_total_sales_question(text: str) -> bool:
        t = (text or "").lower()
        excludes = ["product", "produit", "item", "article", "camera", "specific"]
        if any(ex in t for ex in excludes):
            return False
        return any(kw in t for kw in [
            "total sales", "total turnover", "total revenue",
            "overall sales", "sales summary",
            "chiffre affaires", "ca total",
            "ventes totales", "ventes globales",
            "résumé", "summary",
        ])

    @staticmethod
    def _extract_top_n(text: str) -> int:
        t = text.lower()
        m = re.search(r"top[\s\-]?(\d+)", t)
        if m:
            return min(10, max(1, int(m.group(1))))
        m = re.search(r"(\d+)\s+(?:best|top|selling)", t)
        if m:
            return min(10, max(1, int(m.group(1))))
        return 1

    def _build_response(self, answer: str) -> dict:
        return {
            "answer": answer,
            "decision_needed": False,
            "decision_card": None,
            "suggested_followups": [
                "Should we compare this to the previous period?",
                "Do you want the breakdown by customer or branch?",
                "Which customers drove this revenue?",
            ],
            "urgency": "medium",
            "topic": "revenue",
        }