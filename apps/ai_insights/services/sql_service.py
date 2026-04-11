import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum, Count, Q, Min

from apps.transactions.models import MaterialMovement

logger = logging.getLogger(__name__)



class SQLService:
    SALES_MOVEMENT_KEYWORDS = ["ف بيع", "بيع", "sale", "sell", "sales"]

    def _sales_filter(self):
        return Q(movement_type__in=["ف بيع"]) | Q(movement_type__icontains="بيع")

    def _normalize_decimal(self, value):
        if value is None:
            return 0.0
        if isinstance(value, Decimal):
            return float(value)
        return float(value)

    def get_earliest_transaction_date(self, company):
        """Fetch the earliest transaction date for a company"""
        try:
            earliest = MaterialMovement.objects.filter(
                company=company,
                movement_date__isnull=False
            ).aggregate(earliest_date=Min("movement_date"))
            
            earliest_date = earliest.get("earliest_date")
            if earliest_date:
                return earliest_date if isinstance(earliest_date, date) else earliest_date.date()
            return date(2000, 1, 1)  # Fallback if no data
        except Exception as e:
            logger.warning(f"Error getting earliest transaction date: {e}")
            return date(2000, 1, 1)

    def get_sales_summary(self, company, start_date, end_date):
        base = MaterialMovement.objects.filter(
            company=company,
            movement_date__gte=start_date,
            movement_date__lte=end_date,
        ).filter(self._sales_filter())

        totals = base.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )

        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "total_qty": self._normalize_decimal(totals.get("total_qty")),
            "total_revenue": self._normalize_decimal(totals.get("total_revenue")),
            "transactions": totals.get("transactions") or 0,
        }

    def get_top_sold_products(self, company, start_date, end_date, top_n=5):
        base = MaterialMovement.objects.filter(
            company=company,
            movement_date__gte=start_date,
            movement_date__lte=end_date,
        ).filter(self._sales_filter())

        rows = (
            base.values("material_name")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("-total_qty")[:top_n]
        )

        return [
            {
                "material_name": row.get("material_name") or "Unknown",
                "total_qty": self._normalize_decimal(row.get("total_qty")),
                "total_revenue": self._normalize_decimal(row.get("total_revenue")),
                "transactions": row.get("transactions") or 0,
            }
            for row in rows
        ]

    def get_product_sales(self, company, product_name, start_date, end_date):
        base = MaterialMovement.objects.filter(
            company=company,
            movement_date__gte=start_date,
            movement_date__lte=end_date,
        ).filter(self._sales_filter())

        if product_name:
            base = base.filter(
                Q(material_name__icontains=product_name)
                | Q(material_code__icontains=product_name)
                | Q(product__name__icontains=product_name)
            )

        totals = base.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )

        return {
            "product_name": product_name or "all products",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "total_qty": self._normalize_decimal(totals.get("total_qty")),
            "total_revenue": self._normalize_decimal(totals.get("total_revenue")),
            "transactions": totals.get("transactions") or 0,
        }
