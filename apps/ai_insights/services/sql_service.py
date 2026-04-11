"""
apps/ai_insights/services/sql_service.py
-----------------------------------------
Service SQL pour les requêtes de données de vente.
Supporte :
  - Société unique  (company=...)
  - Multi-sociétés  (companies=[...])
  - Agrégation groupe
"""

import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum, Count, Q, Min

from apps.transactions.models import MaterialMovement

logger = logging.getLogger(__name__)


class SQLService:

    def _sales_filter(self):
        return Q(movement_type__in=["ف بيع"]) | Q(movement_type__icontains="بيع")

    def _normalize_decimal(self, value):
        if value is None:
            return 0.0
        if isinstance(value, Decimal):
            return float(value)
        return float(value)

    # ── Date la plus ancienne ─────────────────────────────────────────────────

    def get_earliest_transaction_date(self, company) -> date:
        try:
            result = (
                MaterialMovement.objects
                .filter(company=company, movement_date__isnull=False)
                .aggregate(earliest_date=Min("movement_date"))
            )
            earliest = result.get("earliest_date")
            if earliest:
                return earliest if isinstance(earliest, date) else earliest.date()
        except Exception as e:
            logger.warning("[SQLService] get_earliest_transaction_date: %s", e)
        return date(2000, 1, 1)

    # ── Résumé global ─────────────────────────────────────────────────────────

    def get_sales_summary(self, company, start_date: date, end_date: date) -> dict:
        """Résumé des ventes pour UNE société."""
        base = (
            MaterialMovement.objects
            .filter(
                company=company,
                movement_date__gte=start_date,
                movement_date__lte=end_date,
            )
            .filter(self._sales_filter())
        )
        totals = base.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )
        return {
            "start_date":    start_date.isoformat(),
            "end_date":      end_date.isoformat(),
            "total_qty":     self._normalize_decimal(totals.get("total_qty")),
            "total_revenue": self._normalize_decimal(totals.get("total_revenue")),
            "transactions":  totals.get("transactions") or 0,
        }

    def get_sales_summary_by_company(
        self, companies: list, start_date: date, end_date: date
    ) -> list:
        """Résumé des ventes par société — pour les questions groupe."""
        results = []
        for company in companies:
            summary = self.get_sales_summary(company, start_date, end_date)
            summary["company_name"] = company.name
            summary["company_id"]   = str(company.id)
            results.append(summary)
        results.sort(key=lambda x: -x["total_revenue"])
        return results

    # ── Top produits ──────────────────────────────────────────────────────────

    def get_top_sold_products(
        self,
        company,
        start_date: date,
        end_date: date,
        top_n: int = 5,
        companies: list = None,
    ) -> list:
        """
        Retourne les top N produits par revenue.
        - company   : société unique
        - companies : liste de sociétés (mode groupe)
        """
        base = (
            MaterialMovement.objects
            .filter(
                movement_date__gte=start_date,
                movement_date__lte=end_date,
            )
            .filter(self._sales_filter())
            .exclude(Q(material_name__isnull=True) | Q(material_name=""))
        )

        if companies:
            base = base.filter(company__in=companies)
        elif company:
            base = base.filter(company=company)

        # Multi-société : grouper par produit ET société
        if companies and len(companies) > 1:
            rows = (
                base
                .values("material_name", "company__name")
                .annotate(
                    total_qty=Sum("qty_out"),
                    total_revenue=Sum("total_out"),
                    transactions=Count("id"),
                )
                .order_by("-total_revenue")[:top_n]
            )
            results = [
                {
                    "material_name": row["material_name"] or "Unknown",
                    "company_name":  row["company__name"] or "",
                    "total_qty":     self._normalize_decimal(row["total_qty"]),
                    "total_revenue": self._normalize_decimal(row["total_revenue"]),
                    "transactions":  row["transactions"] or 0,
                }
                for row in rows
            ]
        else:
            rows = (
                base
                .values("material_name")
                .annotate(
                    total_qty=Sum("qty_out"),
                    total_revenue=Sum("total_out"),
                    transactions=Count("id"),
                )
                .order_by("-total_revenue")[:top_n]
            )
            results = [
                {
                    "material_name": row["material_name"] or "Unknown",
                    "company_name":  company.name if company else "",
                    "total_qty":     self._normalize_decimal(row["total_qty"]),
                    "total_revenue": self._normalize_decimal(row["total_revenue"]),
                    "transactions":  row["transactions"] or 0,
                }
                for row in rows
            ]

        if results and all(r["total_revenue"] == 0 for r in results):
            results.sort(key=lambda r: -r["total_qty"])

        return results

    # ── Produit spécifique ────────────────────────────────────────────────────

    def get_product_sales(
        self, company, product_name: str, start_date: date, end_date: date
    ) -> dict:
        """Ventes d'un produit pour UNE société."""
        base = (
            MaterialMovement.objects
            .filter(
                company=company,
                movement_date__gte=start_date,
                movement_date__lte=end_date,
            )
            .filter(self._sales_filter())
        )
        if product_name:
            base = base.filter(
                Q(material_name__icontains=product_name)
                | Q(material_code__icontains=product_name)
            )
        totals = base.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )
        return {
            "product_name":  product_name or "all products",
            "company_name":  company.name if company else "",
            "start_date":    start_date.isoformat(),
            "end_date":      end_date.isoformat(),
            "total_qty":     self._normalize_decimal(totals.get("total_qty")),
            "total_revenue": self._normalize_decimal(totals.get("total_revenue")),
            "transactions":  totals.get("transactions") or 0,
        }

    def get_product_sales_all_companies(
        self, companies: list, product_name: str, start_date: date, end_date: date
    ) -> list:
        """Ventes d'un produit dans TOUTES les sociétés autorisées."""
        results = []
        for company in companies:
            sales = self.get_product_sales(company, product_name, start_date, end_date)
            if sales["total_revenue"] > 0 or sales["total_qty"] > 0:
                results.append(sales)
        results.sort(key=lambda x: -x["total_revenue"])
        return results