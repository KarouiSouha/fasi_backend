"""
apps/kpi/views_stock.py
-----------------------
Stock KPIs:
  2.1 Niveau de stock (aggregated)
  2.2 Taux de rotation du stock
  2.3 Produits à faible rotation
  2.4 Rupture de stock (zero stock products)
  2.5 Taux de couverture du stock

FORMULA (corrected):
  Taux de rotation = Quantité Vendue / (Stock Initial + Achats)

  Where (all read from transactions_movement for the given year):
    - Stock Initial = SUM(qty_in)  WHERE movement_type = 'ف.أول المدة'
    - Achats        = SUM(qty_in)  WHERE movement_type = 'ف شراء'
    - Quantité vendue = SUM(qty_out) WHERE movement_type = 'ف بيع'

  All three are grouped by material_name (same join key used elsewhere).

FIX: Join sales/opening/purchases → inventory by material_name (NOT material_code).
     Movements file uses short codes (e.g. "EC0020") while inventory uses full codes
     (e.g. "AS-FD-In-AD-EC0020") → code join = 0% match.
     Product names are identical in both files → use name as join key.

FIX2: avg_unit_cost computed in Python (not chained ORM annotation)
      to avoid NameError: unit_cost_sum not defined at queryset eval time.
"""

import logging
from decimal import Decimal
from datetime import date

from django.db.models import Sum, DecimalField
from django.db.models.functions import Coalesce, Trim
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

SALE_TYPES            = ["ف بيع"]
OPENING_BALANCE_TYPES = ["ف.أول المدة"]
PURCHASE_TYPES        = ["ف شراء"]

LOW_ROTATION_THRESHOLD = 0.5
DEFAULT_LEAD_TIME_DAYS = 14
SAFETY_FACTOR          = 0.5


def normalize_key(value: str) -> str:
    """Normalize text for resilient joins/lookup (trim + collapse spaces + lower)."""
    return " ".join((value or "").split()).lower()


class StockKPIView(APIView):
    """
    GET /api/kpi/stock/

    Query params:
        year=<int>                     — year for sales/purchases data (default: current year)
        branch=<str>                   — optional exact branch filter
        low_rotation_threshold=<float> — rotation threshold (default: 0.5)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.inventory.models import InventorySnapshotLine
        from apps.transactions.models import MaterialMovement

        company = request.user.company

        year_param = request.query_params.get("year")
        branch = (request.query_params.get("branch") or "").strip()
        rotation_threshold = float(
            request.query_params.get("low_rotation_threshold", LOW_ROTATION_THRESHOLD)
        )

        branch_normalized = " ".join(branch.split())

        # "Now" behavior: if no explicit year is provided, use the latest
        # available company movement year so branch-scoped data does not
        # accidentally collapse to an older sparse period.
        if year_param:
            year = int(year_param)
        else:
            latest_company_movement_date = (
                MaterialMovement.objects.filter(company=company)
                .order_by("-movement_date")
                .values_list("movement_date", flat=True)
                .first()
            )
            year = (
                latest_company_movement_date.year
                if latest_company_movement_date
                else date.today().year
            )

        period_from = date(year, 1, 1)
        period_to   = date(year, 12, 31)
        n_days      = (period_to - period_from).days + 1

        # ── Base movement queryset for the period ────────────────────────────
        company_mvt = MaterialMovement.objects.filter(
            company=company,
            movement_date__gte=period_from,
            movement_date__lte=period_to,
        )

        branch_mvt = company_mvt
        if branch_normalized:
            branch_mvt = branch_mvt.annotate(
                branch_clean=Trim("branch__name")
            ).filter(branch_clean__iexact=branch_normalized)

        # Prefer branch-scoped movements when they exist; otherwise keep the
        # branch inventory filter and fall back to company movements so the
        # KPI charts remain populated.
        if branch_normalized and branch_mvt.exists():
            base_mvt = branch_mvt
            movement_scope_used = "branch"
        else:
            base_mvt = company_mvt
            movement_scope_used = "company_fallback" if branch_normalized else "company"

        # ── 1. Quantité vendue — grouped by material_name ────────────────────
        sales_qs = (
            base_mvt
            .filter(movement_type__in=SALE_TYPES)
            .values("material_name")
            .annotate(
                qty_sold=Coalesce(Sum("qty_out"), Decimal("0")),
                revenue=Coalesce(Sum("total_out"), Decimal("0")),
            )
        )
        sales_by_name: dict = {}
        for row in sales_qs:
            key = normalize_key(row["material_name"] or "")
            if key:
                sales_by_name[key] = {
                    "qty_sold": float(row["qty_sold"]),
                    "revenue":  float(row["revenue"]),
                }

        # ── 2. Stock Initial — ف.أول المدة, grouped by material_name ─────────
        opening_qs = (
            base_mvt
            .filter(movement_type__in=OPENING_BALANCE_TYPES)
            .values("material_name")
            .annotate(
                qty_opening=Coalesce(Sum("qty_in"), Decimal("0")),
            )
        )
        opening_by_name: dict = {}
        for row in opening_qs:
            key = normalize_key(row["material_name"] or "")
            if key:
                opening_by_name[key] = float(row["qty_opening"])

        # ── 3. Achats — ف شراء, grouped by material_name ────────────────────
        purchase_qs = (
            base_mvt
            .filter(movement_type__in=PURCHASE_TYPES)
            .values("material_name")
            .annotate(
                qty_purchased=Coalesce(Sum("qty_in"), Decimal("0")),
            )
        )
        purchase_by_name: dict = {}
        for row in purchase_qs:
            key = normalize_key(row["material_name"] or "")
            if key:
                purchase_by_name[key] = float(row["qty_purchased"])

        # ── 4. Inventory snapshot lines — grouped by product_name + optional branch ──
        inv_lines = InventorySnapshotLine.objects.filter(company=company)
        if branch_normalized:
            inv_lines = inv_lines.annotate(
                branch_clean=Trim("branch_name")
            ).filter(branch_clean__iexact=branch_normalized)

        inv_lines_list = (
            inv_lines
            .values("product_name", "product_code", "product_category")
            .annotate(
                total_qty=Coalesce(Sum("quantity"), Decimal("0")),
                total_value=Coalesce(Sum("line_value"), Decimal("0")),
                qty_sum=Coalesce(
                    Sum("quantity", output_field=DecimalField()), Decimal("0")
                ),
                unit_cost_sum=Coalesce(
                    Sum("unit_cost", output_field=DecimalField()), Decimal("0")
                ),
            )
        )

        zero_stock_lines = (
            inv_lines
            .values("product_name", "product_code", "product_category", "branch_name")
            .annotate(
                total_qty=Coalesce(Sum("quantity"), Decimal("0")),
                total_value=Coalesce(Sum("line_value"), Decimal("0")),
            )
        )
        
        # ── Debug: log available inventory branches ──────────────────────────
        raw_branches = (
            InventorySnapshotLine.objects.filter(company=company)
            .exclude(branch_name__isnull=True)
            .annotate(branch_clean=Trim("branch_name"))
            .values_list("branch_clean", flat=True)
            .distinct()
        )
        available_branches = sorted(
            {" ".join((b or "").split()) for b in raw_branches if (b or "").strip()}
        )
        logger.info(
            f"Stock KPI: available inventory branches: {available_branches}, "
            f"requested branch: '{branch}', movement_scope_used: {movement_scope_used}"
        )

        # ── 5. Per-product KPIs ──────────────────────────────────────────────
        all_products      = []
        zero_stock        = []
        low_rotation      = []
        total_stock_value = 0.0
        total_stock_qty   = 0.0

        for line in inv_lines_list:
            stock_qty        = float(line["total_qty"])
            stock_val        = float(line["total_value"])
            product_name     = line["product_name"]     or ""
            product_code     = line["product_code"]     or ""
            product_category = line["product_category"] or ""

            # avg_unit_cost in Python to avoid chained-annotation NameError
            qty_sum       = float(line["qty_sum"])
            unit_cost_sum = float(line["unit_cost_sum"])
            cost_price    = (unit_cost_sum / qty_sum) if qty_sum > 0 else 0.0

            total_stock_value += stock_val
            total_stock_qty   += stock_qty

            name_key  = normalize_key(product_name)
            sales     = sales_by_name.get(name_key, {"qty_sold": 0.0, "revenue": 0.0})
            qty_sold  = sales["qty_sold"]

            # ── CORRECTED ROTATION FORMULA ────────────────────────────────────
            # Taux de rotation = Quantité vendue / (Stock Initial + Achats)
            #
            # Stock Initial = opening balance qty (ف.أول المدة)
            # Achats        = purchased qty      (ف شراء)
            #
            # If opening balance and purchases are both 0 for this product
            # (e.g. the product was already in stock before the year started
            # and nothing was bought this year), fall back to the current
            # snapshot stock_qty so the ratio stays meaningful.
            qty_opening  = opening_by_name.get(name_key, 0.0)
            qty_purchased = purchase_by_name.get(name_key, 0.0)
            denominator  = qty_opening + qty_purchased

            if denominator > 0:
                rotation_rate = round(qty_sold / denominator, 4)
            elif stock_qty > 0:
                # Fallback: use current snapshot stock when no movement data
                rotation_rate = round(qty_sold / stock_qty, 4)
            else:
                rotation_rate = 0.0

            # ── Other KPIs ────────────────────────────────────────────────────
            monthly_usage   = qty_sold / 12.0
            safety_stock    = monthly_usage * SAFETY_FACTOR
            min_stock       = int(round(
                monthly_usage * (DEFAULT_LEAD_TIME_DAYS / 30.0) + safety_stock
            ))
            max_stock       = int(round(monthly_usage * 3))
            reorder_qty     = max(0.0, max_stock - stock_qty)

            avg_daily_sales = qty_sold / n_days if n_days > 0 else 0
            coverage_days   = (
                round(stock_qty / avg_daily_sales, 1)
                if avg_daily_sales > 0 else None
            )
            days_of_stock   = (
                round((stock_qty / monthly_usage) * 30)
                if monthly_usage > 0 else None
            )

            if stock_qty == 0:
                stock_status = "out"
            elif min_stock > 0 and stock_qty <= min_stock:
                stock_status = "critical"
            elif min_stock > 0 and stock_qty <= min_stock * 1.5:
                stock_status = "low"
            else:
                stock_status = "ok"

            product_data = {
                "material_code":  product_code,
                "product_name":   product_name,
                "category":       product_category,
                "stock_qty":      stock_qty,
                "stock_value":    stock_val,
                "cost_price":     cost_price,
                # Rotation components (visible in UI for transparency)
                "qty_sold":       qty_sold,
                "qty_opening":    qty_opening,
                "qty_purchased":  qty_purchased,
                "denominator":    denominator,        # Stock Initial + Achats
                "monthly_usage":  round(monthly_usage, 2),
                "revenue":        sales["revenue"],
                "rotation_rate":  rotation_rate,      # corrected formula
                "coverage_days":  coverage_days,
                "min_stock":      min_stock,
                "max_stock":      max_stock,
                "reorder_qty":    round(reorder_qty, 0),
                "days_of_stock":  days_of_stock,
                "status":         stock_status,
            }
            all_products.append(product_data)

            if stock_qty > 0 and rotation_rate < rotation_threshold:
                low_rotation.append({
                    "material_code":  product_code,
                    "product_name":   product_name,
                    "category":       product_category,
                    "stock_qty":      stock_qty,
                    "stock_value":    stock_val,
                    "qty_sold":       qty_sold,
                    "qty_opening":    qty_opening,
                    "qty_purchased":  qty_purchased,
                    "rotation_rate":  rotation_rate,
                    "coverage_days":  coverage_days,
                })

        for line in zero_stock_lines:
            branch_stock_qty = float(line["total_qty"])
            if branch_stock_qty == 0:
                zero_stock.append({
                    "material_code": line["product_code"] or "",
                    "product_name":  line["product_name"] or "",
                    "category":      line["product_category"] or "",
                    "qty_sold":      sales_by_name.get(normalize_key(line["product_name"] or ""), {"qty_sold": 0.0, "revenue": 0.0})["qty_sold"],
                })

        low_rotation.sort(key=lambda x: -x["stock_value"])

        top_rotation = sorted(
            [p for p in all_products if p["stock_qty"] > 0],
            key=lambda x: -x["rotation_rate"]
        )[:20]

        products_with_stock = [p for p in all_products if p["stock_qty"] > 0]
        avg_rotation = (
            sum(p["rotation_rate"] for p in products_with_stock) / len(products_with_stock)
            if products_with_stock else 0.0
        )

        return Response({
            "snapshot_date": None,
            "year":          year,
            "branch_filter": branch or None,
            "movement_scope_used": movement_scope_used,
            "available_branches": available_branches,
            "period":        {"from": str(period_from), "to": str(period_to)},
            "rotation_formula": "qty_sold / (stock_initial + achats)",
            "stock_summary": {
                "total_products":     len(all_products),
                "total_stock_qty":    round(total_stock_qty, 2),
                "total_stock_value":  round(total_stock_value, 2),
                "zero_stock_count":   len(zero_stock),
                "low_rotation_count": len(low_rotation),
                "critical_count":     sum(1 for p in all_products if p["status"] == "critical"),
                "low_count":          sum(1 for p in all_products if p["status"] == "low"),
                "avg_rotation_rate":  round(avg_rotation, 4),
            },
            "top_rotation_products": top_rotation,
            "low_rotation_products": low_rotation[:50],
            "zero_stock_products":   zero_stock,
            "coverage_at_risk": sorted(
                [p for p in products_with_stock if p["coverage_days"] is not None],
                key=lambda x: x["coverage_days"]
            )[:20],
            "reorder_list": sorted(
                all_products,
                key=lambda x: {"out": 0, "critical": 1, "low": 2, "ok": 3}.get(x["status"], 4)
            ),
        })