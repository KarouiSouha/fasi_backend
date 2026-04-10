import uuid

from django.db.models import Q, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.data_import.models import ImportLog
from .models import InventorySnapshotLine
from .serializers import (
    InventorySnapshotSerializer,
    InventorySnapshotListSerializer,
    InventorySnapshotLineSerializer,
)
from apps.branches.resolver import BranchResolver


CURRENT_SNAPSHOT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _get_company(request):
    if request.user.company:
        return request.user.company, None
    if request.user.is_active and (request.user.is_staff or request.user.is_superuser):
        name = request.query_params.get("company_name", "").strip()
        if name:
            from apps.companies.models import Company

            try:
                return Company.objects.get(name=name), None
            except Company.DoesNotExist:
                return None, Response(
                    {"error": "Company not found for provided company_name."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        return None, Response(
            {"error": "Please provide company_name query parameter for admin access."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None, Response(
        {"error": "Your account is not linked to a company."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _safe_int(value, default: int, min_val: int, max_val: int) -> int:
    try:
        return max(min_val, min(max_val, int(value)))
    except (TypeError, ValueError):
        return default


def _resolve_branch_filter_names(company, branch_value: str) -> list[str]:
    """
    Resolve a branch filter coming from the UI to the raw branch_name values
    stored in InventorySnapshotLine.

    The UI works with canonical branch names, while inventory lines keep the
    branch label as imported from Excel. We therefore map canonical → raw
    values before filtering.
    """
    normalized = " ".join((branch_value or "").split()).strip()
    if not normalized:
        return []

    resolver = BranchResolver(company) if company else None
    target_branch = resolver.resolve(normalized) if resolver else None

    raw_names = list(
        InventorySnapshotLine.objects.filter(company=company)
        .values_list("branch_name", flat=True)
        .distinct()
    )

    if target_branch and resolver:
        matching_raw_names: list[str] = []
        for raw_name in raw_names:
            resolved = resolver.resolve(raw_name)
            if resolved and resolved.id == target_branch.id:
              matching_raw_names.append(raw_name)
        if matching_raw_names:
            return matching_raw_names

    # Fallback: case-insensitive comparison on the raw values.
    normalized_lower = normalized.lower()
    return [
        raw_name
        for raw_name in raw_names
        if " ".join((raw_name or "").split()).lower() == normalized_lower
    ]


def _latest_inventory_meta(company):
    return (
        ImportLog.objects.filter(company=company, file_type=ImportLog.FileType.INVENTORY)
        .select_related("imported_by")
        .order_by("-completed_at", "-started_at")
        .first()
    )


def _synthetic_snapshot_payload(company):
    qs = InventorySnapshotLine.objects.filter(company=company)
    totals = qs.aggregate(total_lines_value=Sum("line_value"))
    line_count = qs.count()
    branches = list(qs.values_list("branch_name", flat=True).distinct().order_by("branch_name"))
    latest_year = qs.exclude(inventory_year__isnull=True).values_list("inventory_year", flat=True).order_by("-inventory_year").first()

    meta = _latest_inventory_meta(company)
    uploaded_at = (meta.completed_at or meta.started_at) if meta else None
    source_file = meta.original_filename if meta else ""
    uploaded_by_id = str(meta.imported_by_id) if meta and meta.imported_by_id else None
    uploaded_by_name = None
    if meta and meta.imported_by:
        uploaded_by_name = meta.imported_by.get_full_name() or meta.imported_by.username

    return {
        "id": CURRENT_SNAPSHOT_ID,
        "company_name": company.name,
        "label": "Current Stock",
        "inventory_year": latest_year,
        "snapshot_date": uploaded_at.date() if uploaded_at else None,
        "fiscal_year": str(latest_year) if latest_year else "",
        "source_file": source_file,
        "notes": "",
        "uploaded_at": uploaded_at,
        "uploaded_by": uploaded_by_id,
        "uploaded_by_name": uploaded_by_name,
        "line_count": line_count,
        "total_lines_value": totals["total_lines_value"] or 0,
        "branches": branches,
    }


def _build_lines_response(request, company):
    qs = InventorySnapshotLine.objects.filter(company=company)

    branch = request.query_params.get("branch", "").strip()
    if branch:
        branch_names = _resolve_branch_filter_names(company, branch)
        if branch_names:
            qs = qs.filter(branch_name__in=branch_names)
        else:
            qs = qs.none()

    search = request.query_params.get("search", "").strip()
    if search:
        qs = qs.filter(
            Q(product_name__icontains=search) | Q(product_code__icontains=search)
        )

    totals = qs.aggregate(
        grand_total_qty=Sum("quantity"),
        grand_total_value=Sum("line_value"),
    )
    distinct_products = qs.values("product_code").distinct().count()
    out_of_stock_count = qs.filter(quantity=0).count()
    critical_count = qs.filter(quantity__gt=0, quantity__lt=30).count()
    low_count = qs.filter(quantity__gte=30, quantity__lte=50).count()
    total_lines = qs.count()

    page = _safe_int(request.query_params.get("page", 1), default=1, min_val=1, max_val=10_000)
    page_size = _safe_int(request.query_params.get("page_size", 100), default=100, min_val=1, max_val=500)
    qs_page = qs[(page - 1) * page_size: page * page_size]

    return {
        "snapshot_id": str(CURRENT_SNAPSHOT_ID),
        "count": total_lines,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total_lines + page_size - 1) // page_size),
        "totals": {
            "grand_total_qty": float(totals["grand_total_qty"] or 0),
            "grand_total_value": float(totals["grand_total_value"] or 0),
            "distinct_products": distinct_products,
            "out_of_stock_count": out_of_stock_count,
            "critical_count": critical_count,
            "low_count": low_count,
        },
        "lines": InventorySnapshotLineSerializer(qs_page, many=True).data,
    }


class InventoryListView(APIView):
    """
    GET /api/inventory/
    Returns a single synthetic session representing current stock.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company, err = _get_company(request)
        if err:
            return err

        item = _synthetic_snapshot_payload(company)
        page = _safe_int(request.query_params.get("page", 1), default=1, min_val=1, max_val=10_000)
        page_size = _safe_int(request.query_params.get("page_size", 20), default=20, min_val=1, max_val=100)
        items = [item] if page == 1 else []

        return Response({
            "count": 1,
            "page": page,
            "page_size": page_size,
            "total_pages": 1,
            "items": InventorySnapshotListSerializer(items, many=True).data,
        })


class InventoryDetailView(APIView):
    """GET /api/inventory/<uuid:snapshot_id>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, snapshot_id):
        company, err = _get_company(request)
        if err:
            return err

        payload = _synthetic_snapshot_payload(company)
        return Response(InventorySnapshotSerializer(payload).data)

    def delete(self, request, snapshot_id):
        company, err = _get_company(request)
        if err:
            return err

        InventorySnapshotLine.objects.filter(company=company).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InventorySnapshotLinesView(APIView):
    """
    GET /api/inventory/<uuid:snapshot_id>/lines/
    Supports ?branch=, ?search=, ?page=, ?page_size=

    IMPORTANT: All totals (grand_total_qty, grand_total_value, distinct_products,
    out_of_stock_count, critical_count, low_count) are computed on the FULL
    filtered queryset BEFORE pagination, so they always reflect the correct
    counts for the selected branch.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, snapshot_id):
        company, err = _get_company(request)
        if err:
            return err
        return Response(_build_lines_response(request, company))


class InventoryLinesView(APIView):
    """GET /api/inventory/lines/ — paginated current stock lines."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        company, err = _get_company(request)
        if err:
            return err
        return Response(_build_lines_response(request, company))


class InventoryBranchSummaryView(APIView):
    """
    GET /api/inventory/branch-summary/
    Supports ?snapshot_id= and ?branch= filters.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company, err = _get_company(request)
        if err:
            return err

        qs = InventorySnapshotLine.objects.filter(
            company=company,
        )

        branch = request.query_params.get("branch", "").strip()
        if branch:
            branch_names = _resolve_branch_filter_names(company, branch)
            if branch_names:
                qs = qs.filter(branch_name__in=branch_names)
            else:
                qs = qs.none()

        rows = (
            qs.values("branch_name")
            .annotate(
                total_qty=Sum("quantity"),
                total_value=Sum("line_value"),
            )
            .order_by("branch_name")
        )

        # Normalise via BranchResolver
        company  = request.user.company
        resolver = BranchResolver(company) if company else None

        merged: dict[str, dict] = {}
        for r in rows:
            raw = r["branch_name"] or "Unknown"
            if resolver:
                branch_obj = resolver.resolve(raw)
                canonical  = branch_obj.name if branch_obj else raw
            else:
                canonical = raw

            qty   = float(r["total_qty"]   or 0)
            value = float(r["total_value"] or 0)

            if canonical in merged:
                merged[canonical]["total_qty"]   += qty
                merged[canonical]["total_value"] += value
            else:
                merged[canonical] = {"total_qty": qty, "total_value": value}

        return Response({
            "branches": [
                {
                    "branch":      name,
                    "total_qty":   data["total_qty"],
                    "total_value": data["total_value"],
                }
                for name, data in sorted(merged.items())
            ],
        })


class InventoryCategoryBreakdownView(APIView):
    """
    GET /api/inventory/category-breakdown/
    Supports ?snapshot_id= and ?branch= filters.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company, err = _get_company(request)
        if err:
            return err

        qs = InventorySnapshotLine.objects.filter(
            company=company,
        )

        branch = request.query_params.get("branch", "").strip()
        if branch:
            branch_names = _resolve_branch_filter_names(company, branch)
            if branch_names:
                qs = qs.filter(branch_name__in=branch_names)
            else:
                qs = qs.none()

        breakdown = (
            qs.values("product_category")
            .annotate(
                total_qty=Sum("quantity"),
                total_value=Sum("line_value"),
            )
            .order_by("-total_value")
        )

        return Response({
            "categories": [
                {
                    "category":    r["product_category"] or "Uncategorized",
                    "total_qty":   float(r["total_qty"]   or 0),
                    "total_value": float(r["total_value"] or 0),
                }
                for r in breakdown
            ],
        })


class InventorySnapshotDatesView(APIView):
    """GET /api/inventory/dates/ — distinct import dates."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company, err = _get_company(request)
        if err:
            return err

        dates = (
            ImportLog.objects
            .filter(company=company, file_type=ImportLog.FileType.INVENTORY)
            .values_list("completed_at", flat=True)
            .distinct()
            .order_by("-completed_at")
        )
        return Response({"dates": [d.date().isoformat() for d in dates if d]})