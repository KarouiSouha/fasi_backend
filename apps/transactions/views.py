from decimal import Decimal

from django.db.models import Q, Sum, Count, F, Value, DecimalField, ExpressionWrapper
from django.db.models.functions import TruncMonth, Coalesce
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MaterialMovement
from .serializers import (
    MovementListSerializer,
    MovementDetailSerializer,
)

CALENDAR_MONTHS = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# Arabic movement type groups for aggregation
SALE_TYPES            = ["ف بيع"]
PURCHASE_TYPES        = ["ف شراء"]
RETURN_SALE_TYPES     = ["مردودات بيع"]
RETURN_PURCHASE_TYPES = ["مردود شراء"]


def _strip_param(request, key: str, default: str = "") -> str:
    """
    Read a query-string parameter and strip ALL whitespace from both ends.

    This guards against clients (or Excel-sourced values) accidentally sending
    a movement_type like 'ف بيع ' (with trailing space), which would produce
    zero results even though the DB contains 'ف بيع'.
    """
    return request.query_params.get(key, default).strip()


def _apply_date_branch_filters(qs, request):
    """
    Shared helper — applies date_from, date_to, year, branch filters.
    Used by Summary, TypeBreakdown, BranchBreakdown, BranchMonthly.
    ✅ branch uses iexact for exact name matching (avoids false positives
       when e.g. "Tripoli" would match "Tripoli Est" with icontains).
    """
    date_from = _strip_param(request, "date_from")
    if date_from:
        qs = qs.filter(movement_date__gte=date_from)

    date_to = _strip_param(request, "date_to")
    if date_to:
        qs = qs.filter(movement_date__lte=date_to)

    # ✅ Exact branch filter
    branch = _strip_param(request, "branch")
    if branch:
        qs = qs.filter(branch__name__iexact=branch)

    # Year filter only when no date range is provided
    year = _strip_param(request, "year")
    if year and not date_from and not date_to:
        try:
            qs = qs.filter(movement_date__year=int(year))
        except ValueError:
            pass

    return qs


class TransactionListView(APIView):
    """
    GET /api/transactions/

    Query params:
        movement_type=<arabic_value>   — exact match on raw Arabic label
        branch=<str>                   — branch.name iexact
        search=<str>                   — material_code / material_name / customer_name
        date_from=YYYY-MM-DD
        date_to=YYYY-MM-DD
        ordering=<field>
        page=<int>   page_size=<int>   — default 50, max 200
    """

    permission_classes = [IsAuthenticated]
    ALLOWED_ORDERINGS = {
        "movement_date", "-movement_date",
        "material_code", "-material_code",
        "total_in", "-total_in",
        "total_out", "-total_out",
        "created_at", "-created_at",
    }

    def get(self, request):
        qs = MaterialMovement.objects.filter(
            company=request.user.company
        ).select_related("product", "branch", "customer")

        # ── FIX: always .strip() the movement_type param ─────────────────────
        movement_type = _strip_param(request, "movement_type")
        if movement_type:
            qs = qs.filter(movement_type=movement_type)

        # ✅ Exact branch filter
        branch = _strip_param(request, "branch")
        if branch:
            qs = qs.filter(branch__name__iexact=branch)

        search = _strip_param(request, "search")
        if search:
            qs = qs.filter(
                Q(material_code__icontains=search) |
                Q(material_name__icontains=search) |
                Q(customer_name__icontains=search) |
                Q(lab_code__icontains=search)
            )

        date_from = _strip_param(request, "date_from")
        if date_from:
            qs = qs.filter(movement_date__gte=date_from)

        date_to = _strip_param(request, "date_to")
        if date_to:
            qs = qs.filter(movement_date__lte=date_to)

        ordering = request.query_params.get("ordering", "-movement_date")
        if ordering in self.ALLOWED_ORDERINGS:
            qs = qs.order_by(ordering)

        # ── Totals computed on the full filtered queryset ─────────────────────
        sale_total_out = (
            qs.filter(movement_type="ف بيع")
            .aggregate(v=Sum("total_out"))["v"] or 0
        )
        purchase_total_in = (
            qs.filter(movement_type="ف شراء")
            .aggregate(v=Sum("total_in"))["v"] or 0
        )
        totals = {
            "total_in_value":  float(purchase_total_in),
            "total_out_value": float(sale_total_out),
        }

        total_count = qs.count()
        page      = max(1, int(request.query_params.get("page", 1)))
        page_size = min(200, max(1, int(request.query_params.get("page_size", 50))))
        start     = (page - 1) * page_size
        qs_page   = qs[start: start + page_size]

        return Response({
            "count":       total_count,
            "page":        page,
            "page_size":   page_size,
            "total_pages": max(1, (total_count + page_size - 1) // page_size),
            "totals":      totals,
            "movements":   MovementListSerializer(qs_page, many=True).data,
        })


class TransactionDetailView(APIView):
    """GET /api/transactions/{id}/"""

    permission_classes = [IsAuthenticated]

    def get(self, request, movement_id):
        try:
            movement = MaterialMovement.objects.select_related(
                "product", "branch", "customer"
            ).get(id=movement_id, company=request.user.company)
        except MaterialMovement.DoesNotExist:
            return Response(
                {"error": "Movement not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(MovementDetailSerializer(movement).data)


class TransactionMovementTypesView(APIView):
    """
    GET /api/transactions/movement-types/
    Returns distinct movement_type values for this company.
    Values are trimmed in the DB (via migration 0003) and at import time,
    so no post-processing is needed here.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        types = (
            MaterialMovement.objects
            .filter(company=request.user.company)
            .exclude(movement_type="")
            .values_list("movement_type", flat=True)
            .distinct()
            .order_by("movement_type")
        )
        return Response({"movement_types": list(types)})


class TransactionBranchesView(APIView):
    """
    GET /api/transactions/branches/
    Returns distinct branch names (canonical, normalised at import time).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        branches = (
            MaterialMovement.objects
            .filter(company=request.user.company)
            .exclude(branch__name__isnull=True)
            .exclude(branch__name="")
            .values_list("branch__name", flat=True)
            .distinct()
            .order_by("branch__name")
        )
        return Response({"branches": sorted(set(branches))})


class TransactionSummaryView(APIView):
    """
    GET /api/transactions/summary/
    Monthly aggregated sales vs purchases.

    Query params:
        year=<int>
        date_from=YYYY-MM-DD
        date_to=YYYY-MM-DD
        branch=<str>          ✅ NEW — filters by branch before aggregating
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = MaterialMovement.objects.filter(company=request.user.company)

        zero_decimal = Value(
            Decimal("0.0000"),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        )
        profit_expression = ExpressionWrapper(
            (
                Coalesce(F("price_out"), zero_decimal)
                - Coalesce(F("balance_price"), zero_decimal)
            )
            * Coalesce(F("qty_out"), zero_decimal),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        )

        # ✅ Apply date + branch filters via shared helper
        qs = _apply_date_branch_filters(qs, request)

        monthly = (
            qs.annotate(month=TruncMonth("movement_date"))
            .values("month", "movement_type")
            .annotate(
                total_out_value=Sum("total_out"),
                total_in_value=Sum("total_in"),
                total_profit=Sum(profit_expression),
                qty_out_value=Sum("qty_out"),
                row_count=Count("id"),
            )
            .order_by("month")
        )

        pivot = {}
        for row in monthly:
            key = row["month"].strftime("%Y-%m")
            if key not in pivot:
                pivot[key] = {
                    "year":            row["month"].year,
                    "month":           row["month"].month,
                    "month_label":     CALENDAR_MONTHS[row["month"].month],
                    "total_sales":     0,
                    "total_purchases": 0,
                    "total_profit":    0,
                    "total_qty":       0,
                    "sales_count":     0,
                    "purchases_count": 0,
                }
            # ── FIX: strip movement_type from DB row before comparing ─────────
            mt = (row["movement_type"] or "").strip()
            if mt in SALE_TYPES or mt in RETURN_SALE_TYPES:
                pivot[key]["total_sales"]  += float(row["total_out_value"] or 0)
                pivot[key]["total_profit"] += float(row["total_profit"] or 0)
                pivot[key]["total_qty"]    += float(row["qty_out_value"] or 0)
                pivot[key]["sales_count"]  += row["row_count"]
            elif mt in PURCHASE_TYPES or mt in RETURN_PURCHASE_TYPES:
                pivot[key]["total_purchases"] += float(row["total_in_value"] or 0)
                pivot[key]["purchases_count"] += row["row_count"]

        return Response({"summary": list(pivot.values())})


class TransactionYearsView(APIView):
    """
    GET /api/transactions/years/
    Returns the distinct years present in transaction movement dates.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.transactions.models import MaterialMovement

        company = request.user.company
        years = (
            MaterialMovement.objects
            .filter(company=company, movement_date__isnull=False)
            .values_list('movement_date__year', flat=True)
            .distinct()
            .order_by('-movement_date__year')
        )
        return Response({'years': list(years)})


class TransactionTypeBreakdownView(APIView):
    """
    GET /api/transactions/type-breakdown/
    Totals grouped by movement_type (raw Arabic label).

    Query params:
        date_from=YYYY-MM-DD
        date_to=YYYY-MM-DD
        branch=<str>          ✅ NEW
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = MaterialMovement.objects.filter(company=request.user.company)

        # ✅ Apply date + branch filters
        qs = _apply_date_branch_filters(qs, request)

        breakdown = (
            qs.values("movement_type")
            .annotate(
                count=Count("id"),
                total_in=Sum("total_in"),
                total_out=Sum("total_out"),
            )
            .order_by("-count")
        )

        return Response({
            "breakdown": [
                {
                    # ── FIX: strip movement_type from DB before returning ──────
                    "movement_type": (row["movement_type"] or "").strip(),
                    "count":         row["count"],
                    "total_in":      float(row["total_in"]  or 0),
                    "total_out":     float(row["total_out"] or 0),
                }
                for row in breakdown
            ]
        })


class TransactionBranchBreakdownView(APIView):
    """
    GET /api/transactions/branch-breakdown/

    Query params:
        movement_type=<arabic_value>   — default: "ف بيع"
        year=<int>
        date_from / date_to
    """

    permission_classes = [IsAuthenticated]

    PURCHASE_TYPES = {"ف شراء", "مردود شراء"}

    def get(self, request):
        # ── FIX: strip the incoming movement_type param ───────────────────────
        movement_type = _strip_param(request, "movement_type", "ف بيع")

        qs = MaterialMovement.objects.filter(
            company=request.user.company,
            movement_type=movement_type,
        )

        # ✅ Apply date filters (branch not applied here — this view aggregates BY branch)
        date_from = _strip_param(request, "date_from")
        if date_from:
            qs = qs.filter(movement_date__gte=date_from)

        date_to = _strip_param(request, "date_to")
        if date_to:
            qs = qs.filter(movement_date__lte=date_to)

        year = _strip_param(request, "year")
        if year and not date_from and not date_to:
            try:
                qs = qs.filter(movement_date__year=int(year))
            except ValueError:
                pass

        value_field = "total_in" if movement_type in self.PURCHASE_TYPES else "total_out"

        zero_decimal = Value(
            Decimal("0.0000"),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        )
        profit_expression = ExpressionWrapper(
            (
                Coalesce(F("price_out"), zero_decimal)
                - Coalesce(F("balance_price"), zero_decimal)
            )
            * Coalesce(F("qty_out"), zero_decimal),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        )

        breakdown = (
            qs.values("branch__name")
            .annotate(
                count=Count("id"),
                total=Sum(value_field),
                total_profit=Sum(profit_expression),
            )
            .order_by("-total")
        )

        return Response({
            "movement_type": movement_type,
            "branches": [
                {
                    "branch":        row["branch__name"] or "Unknown",
                    "count":         row["count"],
                    "total":         float(row["total"] or 0),
                    "total_profit":  float(row["total_profit"] or 0),
                }
                for row in breakdown
            ],
        })


class TransactionBranchMonthlyView(APIView):
    """
    GET /api/transactions/branch-monthly/
    Monthly sales breakdown per branch — powers the per-branch line chart.

    Query params:
        movement_type=<arabic_value>  — default: "ف بيع"
        metric=<revenue|profit>       — default: revenue
        year=<int>
        date_from=YYYY-MM-DD
        date_to=YYYY-MM-DD
    """

    permission_classes = [IsAuthenticated]

    PURCHASE_TYPES = {"ف شراء", "مردود شراء", "ادخال رئيسي"}

    def get(self, request):
        # ── FIX: strip the incoming movement_type param ───────────────────────
        movement_type = _strip_param(request, "movement_type", "ف بيع")
        metric    = _strip_param(request, "metric", "revenue").lower()
        year      = _strip_param(request, "year")
        date_from = _strip_param(request, "date_from")
        date_to   = _strip_param(request, "date_to")

        qs = MaterialMovement.objects.filter(
            company=request.user.company,
            movement_type=movement_type,
        )

        if date_from:
            qs = qs.filter(movement_date__gte=date_from)
        if date_to:
            qs = qs.filter(movement_date__lte=date_to)
        if year and not date_from and not date_to:
            try:
                qs = qs.filter(movement_date__year=int(year))
            except ValueError:
                pass

        zero_decimal = Value(
            Decimal("0.0000"),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        )
        profit_expression = ExpressionWrapper(
            (
                Coalesce(F("price_out"), zero_decimal)
                - Coalesce(F("balance_price"), zero_decimal)
            )
            * Coalesce(F("qty_out"), zero_decimal),
            output_field=DecimalField(max_digits=18, decimal_places=4),
        )

        if metric == "profit":
            value_annotation = Sum(profit_expression)
        else:
            value_field = "total_in" if movement_type in self.PURCHASE_TYPES else "total_out"
            value_annotation = Sum(value_field)

        rows = (
            qs.annotate(month=TruncMonth("movement_date"))
            .values("month", "branch__name")
            .annotate(total=value_annotation, count=Count("id"))
            .order_by("month", "branch__name")
        )

        pivot: dict = {}
        branches: set[str] = set()

        for row in rows:
            key    = row["month"].strftime("%Y-%m")
            branch = row["branch__name"] or "Unknown"
            branches.add(branch)
            if key not in pivot:
                pivot[key] = {
                    "month":     CALENDAR_MONTHS[row["month"].month],
                    "year":      row["month"].year,
                    "_sort_key": key,
                }
            pivot[key][branch] = float(row["total"] or 0)

        # ── FIX: fill 0 for every branch in every month ───────────────────────
        # When a branch has no sales in a given month, that month's pivot entry
        # has no key for that branch.  Recharts treats a missing key as null /
        # undefined and — with connectNulls=false (the default) — breaks the
        # line at that point.  The result: a branch with data only in Jan, Feb,
        # May looks like it "disappears" after Feb even though May data exists.
        #
        # Filling the gap with 0.0 means every month always has a value for
        # every branch, so the line stays continuous (drops to 0 between
        # active months instead of breaking).
        all_branches = sorted(list(branches))
        for entry in pivot.values():
            for b in all_branches:
                if b not in entry and b not in ("month", "year", "_sort_key"):
                    entry[b] = 0.0

        sorted_data = sorted(pivot.values(), key=lambda x: x["_sort_key"])
        for item in sorted_data:
            item.pop("_sort_key", None)

        return Response({
            "movement_type": movement_type,
            "metric":        metric,
            "branches":      all_branches,
            "monthly_data":  sorted_data,
        })