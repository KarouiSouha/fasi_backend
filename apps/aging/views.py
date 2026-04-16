from django.db.models import Q, Sum, Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AgingReceivable, AgingSnapshot
from .serializers import AgingReceivableSerializer, AgingListSerializer, AgingSnapshotSerializer

AGING_BUCKETS = [
    ("current", "Current", 0),
    ("d1_30", "1-30d", 15),
    ("d31_60", "31-60d", 45),
    ("d61_90", "61-90d", 75),
    ("d91_120", "91-120d", 105),
    ("d121_150", "121-150d", 135),
    ("d151_180", "151-180d", 165),
    ("d181_210", "181-210d", 195),
    ("d211_240", "211-240d", 225),
    ("d241_270", "241-270d", 255),
    ("d271_300", "271-300d", 285),
    ("d301_330", "301-330d", 315),
    ("over_330", ">330d", 400),
]
GROWTH_COMPARISON_YEARS = 5   # fenêtre de comparaison


def _strip_param(request, key: str, default: str = "") -> str:
    return request.query_params.get(key, default).strip()


def _build_sales_map(company) -> dict:
    from apps.transactions.models import MaterialMovement

    qs = (
        MaterialMovement.objects
        .filter(company=company, movement_type="ف بيع")
        .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
        .exclude(Q(branch__name__isnull=True) | Q(branch__name=""))
        .values_list("customer_name", "branch__name")
        .distinct()
    )
    sales_map = {}
    for cname, branch_name in qs:
        if cname and cname not in sales_map:
            sales_map[cname] = branch_name
    return sales_map


def _resolve_branch(record: AgingReceivable, sales_map: dict):
    cname = record.customer.name if record.customer else None
    if cname and cname in sales_map:
        return sales_map[cname]
    return None


def _get_snapshot_and_qs(
    company,
    snapshot_id_param: str,
    report_date_param: str = None,
    aging_year_param: str = None,
):
    """
    Returns (snapshot, qs).
    - If snapshot_id_param given, fetch that specific snapshot (404 if missing).
    - If aging_year_param is provided, resolve the latest snapshot for that aging year.
    - If report_date_param is provided, resolve by report_date or uploaded_at date.
    - Otherwise defaults to the latest snapshot for the company.
    Returns (None, None)          → caller should return 404.
    Returns (None, empty_qs)      → company has no snapshots yet (return empty data).
    Returns (snapshot, qs)        → normal path.
    """
    snapshot = None
    if snapshot_id_param:
        try:
            snapshot = AgingSnapshot.objects.get(id=snapshot_id_param, company=company)
        except AgingSnapshot.DoesNotExist:
            return None, None
    elif aging_year_param:
        try:
            year = int(aging_year_param)
            snapshot = (
                AgingSnapshot.objects
                .filter(company=company, aging_year=year)
                .order_by("-uploaded_at")
                .first()
            )
        except (TypeError, ValueError):
            snapshot = None
    elif report_date_param:
        snapshot = (
            AgingSnapshot.objects
            .filter(company=company)
            .filter(
                Q(report_date=report_date_param) |
                Q(report_date__isnull=True, uploaded_at__date=report_date_param)
            )
            .order_by("-uploaded_at")
            .first()
        )
    else:
        snapshot = AgingSnapshot.objects.filter(company=company).order_by("-uploaded_at").first()

    if snapshot is None:
        return None, AgingReceivable.objects.none()

    return snapshot, AgingReceivable.objects.filter(snapshot=snapshot)


class AgingSnapshotListView(APIView):
    """
    GET  /api/aging/snapshots/           → list all snapshots (newest first)
    DELETE /api/aging/snapshots/<uuid>/  → delete (roll back) a snapshot
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company
        snapshots = AgingSnapshot.objects.filter(company=company).order_by("-uploaded_at")
        data = AgingSnapshotSerializer(snapshots, many=True).data
        return Response({"count": len(data), "items": data})

    def delete(self, request, snapshot_id):
        company = request.user.company
        try:
            snapshot = AgingSnapshot.objects.get(id=snapshot_id, company=company)
        except AgingSnapshot.DoesNotExist:
            return Response({"error": "Snapshot not found."}, status=status.HTTP_404_NOT_FOUND)
        snapshot.delete()
        return Response({"deleted": str(snapshot_id)}, status=status.HTTP_200_OK)


class AgingListView(APIView):
    permission_classes = [IsAuthenticated]
    ALLOWED_ORDERINGS = {
        "total", "-total",
        "account_code", "-account_code",
        "account", "-account",
        "created_at", "-created_at",
    }

    def get(self, request):
        company = request.user.company
        snapshot_id_param = _strip_param(request, "snapshot_id")
        report_date_param = _strip_param(request, "report_date")
        aging_year_param = _strip_param(request, "aging_year")

        snapshot, qs_all = _get_snapshot_and_qs(
            company,
            snapshot_id_param,
            report_date_param,
            aging_year_param,
        )
        if snapshot is None and snapshot_id_param:
            return Response({"error": "Snapshot not found."}, status=status.HTTP_404_NOT_FOUND)

        total_accounts = qs_all.count()
        credit_customers = qs_all.filter(total__gt=0).count()

        qs = qs_all.filter(total__gt=0).select_related("customer")

        search = _strip_param(request, "search")
        if search:
            qs = qs.filter(Q(account__icontains=search) | Q(account_code__icontains=search))

        risk_filter = _strip_param(request, "risk").lower()

        ordering = request.query_params.get("ordering", "-total")
        if ordering in self.ALLOWED_ORDERINGS:
            qs = qs.order_by(ordering)

        totals = qs.aggregate(grand_total=Sum("total"))

        sales_map = _build_sales_map(company)
        all_records = list(qs)
        if risk_filter in ("low", "medium", "high", "critical"):
            all_records = [r for r in all_records if r.risk_score == risk_filter]

        total_count = len(all_records)
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(200, max(1, int(request.query_params.get("page_size", 200))))
        start = (page - 1) * page_size
        page_records = all_records[start: start + page_size]

        serialized = AgingListSerializer(page_records, many=True).data
        for i, record in enumerate(page_records):
            serialized[i]["branch"] = _resolve_branch(record, sales_map)

        return Response({
            "snapshot_id": str(snapshot.id) if snapshot else None,
            "report_date": str(snapshot.report_date or snapshot.uploaded_at.date()) if snapshot else None,
            "uploaded_at": snapshot.uploaded_at.isoformat() if snapshot else None,
            "total_accounts": total_accounts,
            "count": total_count,
            "credit_customers": credit_customers,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total_count + page_size - 1) // page_size),
            "grand_total": float(totals["grand_total"] or 0),
            "records": serialized,
        })


class AgingDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, aging_id):
        try:
            record = AgingReceivable.objects.select_related("customer").get(
                id=aging_id,
                company=request.user.company,
            )
        except AgingReceivable.DoesNotExist:
            return Response({"error": "Aging record not found."}, status=status.HTTP_404_NOT_FOUND)

        data = AgingReceivableSerializer(record).data
        sales_map = _build_sales_map(request.user.company)
        data["branch"] = _resolve_branch(record, sales_map)
        return Response(data)


class AgingRiskView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company
        snapshot_id_param = _strip_param(request, "snapshot_id")
        report_date_param = _strip_param(request, "report_date")
        aging_year_param = _strip_param(request, "aging_year")

        snapshot, qs_all = _get_snapshot_and_qs(
            company,
            snapshot_id_param,
            report_date_param,
            aging_year_param,
        )
        if snapshot is None and snapshot_id_param:
            return Response({"error": "Snapshot not found."}, status=status.HTTP_404_NOT_FOUND)

        qs = qs_all.select_related("customer").order_by("-total")

        risk_filter = _strip_param(request, "risk").lower()
        limit = min(100, max(1, int(request.query_params.get("limit", 20))))

        records = list(qs)
        if risk_filter in ("low", "medium", "high", "critical"):
            records = [r for r in records if r.risk_score == risk_filter]
        else:
            records = [r for r in records if r.risk_score != "low"]

        records = records[:limit]
        sales_map = _build_sales_map(company)

        return Response({
            "snapshot_id": str(snapshot.id) if snapshot else None,
            "count": len(records),
            "top_risk": [
                {
                    "id": str(r.id),
                    "account": r.account,
                    "account_code": r.account_code,
                    "customer_name": (r.customer.name if r.customer else None),
                    "branch": _resolve_branch(r, sales_map),
                    "total": float(r.total),
                    "overdue_total": float(r.overdue_total),
                    "risk_score": r.risk_score,
                }
                for r in records
            ],
        })


class AgingDistributionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company
        snapshot_id_param = _strip_param(request, "snapshot_id")
        report_date_param = _strip_param(request, "report_date")
        aging_year_param = _strip_param(request, "aging_year")

        snapshot, qs = _get_snapshot_and_qs(
            company,
            snapshot_id_param,
            report_date_param,
            aging_year_param,
        )
        if snapshot is None and snapshot_id_param:
            return Response({"error": "Snapshot not found."}, status=status.HTTP_404_NOT_FOUND)

        agg_fields = {field: Sum(field) for field, _, _ in AGING_BUCKETS}
        totals = qs.aggregate(**agg_fields)

        grand_total = sum(float(totals[f] or 0) for f, _, _ in AGING_BUCKETS)

        distribution = [
            {
                "bucket": field,
                "label": label,
                "total": float(totals[field] or 0),
                "percentage": round(float(totals[field] or 0) / grand_total * 100, 2) if grand_total else 0,
                "midpoint_days": midpoint,
            }
            for field, label, midpoint in AGING_BUCKETS
        ]

        return Response({"grand_total": round(grand_total, 2), "distribution": distribution})


class AgingReportDatesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        snapshots = (
            AgingSnapshot.objects
            .filter(company=request.user.company)
            .order_by("-uploaded_at")
            .values("id", "report_date", "uploaded_at")
        )
        dates = [
            str(s["report_date"] or s["uploaded_at"].date())
            for s in snapshots
        ]
        return Response({"dates": dates})


class AgingHistoricalTrendView(APIView):
    """
    GET /api/aging/historical-trend/

    ✅ Query params:
        date_from=YYYY-MM-DD  — only include snapshots on or after this date
        date_to=YYYY-MM-DD    — only include snapshots on or before this date

    Each point in the response corresponds to one AgingSnapshot.
    Filtering is done on the snapshot's report_date (or uploaded_at when
    report_date is null), so the period filter from the frontend directly
    controls which snapshots appear in the chart.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company

        # ✅ Date range filter — applied to snapshot report_date / uploaded_at
        date_from = _strip_param(request, "date_from")
        date_to   = _strip_param(request, "date_to")

        snapshots_qs = (
            AgingSnapshot.objects
            .filter(company=company)
            .order_by("uploaded_at")
        )

        # ✅ Filter snapshots by date range using report_date when available,
        # falling back to uploaded_at date when report_date is null.
        if date_from:
            snapshots_qs = snapshots_qs.filter(
                # report_date is set AND >= date_from
                # OR report_date is null AND uploaded_at.date >= date_from
                Q(report_date__isnull=False, report_date__gte=date_from) |
                Q(report_date__isnull=True,  uploaded_at__date__gte=date_from)
            )

        if date_to:
            snapshots_qs = snapshots_qs.filter(
                Q(report_date__isnull=False, report_date__lte=date_to) |
                Q(report_date__isnull=True,  uploaded_at__date__lte=date_to)
            )

        result = []
        for snap in snapshots_qs:
            agg = snap.lines.aggregate(
                total_amount=Sum("total"),
                current_amount=Sum("current"),
                total_customers=Count("id"),
            )

            total_amount    = float(agg["total_amount"]  or 0)
            current_amount  = float(agg["current_amount"] or 0)
            total_customers = agg["total_customers"] or 0

            if total_amount == 0:
                continue

            overdue_amount = total_amount - current_amount

            # Paid customers = no overdue buckets at all
            paid_customers = snap.lines.filter(
                d1_30=0, d31_60=0, d61_90=0, d91_120=0,
                d121_150=0, d151_180=0, d181_210=0, d211_240=0,
                d241_270=0, d271_300=0, d301_330=0, over_330=0,
            ).count()

            # Customers with overdue > 60 days specifically
            overdue60_customers = snap.lines.filter(
                Q(d61_90__gt=0) | Q(d91_120__gt=0) | Q(d121_150__gt=0) |
                Q(d151_180__gt=0) | Q(d181_210__gt=0) | Q(d211_240__gt=0) |
                Q(d241_270__gt=0) | Q(d271_300__gt=0) | Q(d301_330__gt=0) |
                Q(over_330__gt=0)
            ).count()

            overdue_customers = total_customers - paid_customers

            result.append({
                "snapshot_id":         str(snap.id),
                "label":               str(snap.report_date or snap.uploaded_at.date()),
                "total_amount":        round(total_amount, 2),
                "current_amount":      round(current_amount, 2),
                "overdue_amount":      round(overdue_amount, 2),
                "collected_rate":      round(current_amount / total_amount * 100, 1),
                "overdue_rate":        round(overdue_amount / total_amount * 100, 1),
                "total_customers":     total_customers,
                "paid_customers":      paid_customers,
                "overdue_customers":   overdue_customers,
                "overdue60_customers": overdue60_customers,
                "paid_pct":            round(paid_customers      / total_customers * 100, 1) if total_customers else 0,
                "overdue_pct":         round(overdue_customers   / total_customers * 100, 1) if total_customers else 0,
                "overdue60_pct":       round(overdue60_customers / total_customers * 100, 1) if total_customers else 0,
            })

        return Response({"count": len(result), "trend": result})


class AgingCustomerGrowthView(APIView):
    """
    GET /api/aging/customer-growth/
 
    Calcule le nombre de clients par année (un snapshot par an) et ajoute :
 
      growth_rate          — % vs année précédente  (inchangé, pour le tableau)
      growth_rate_vs_5y    — % entre l'année courante et (année courante − 5)
                             NULL si l'année de référence n'est pas disponible
      is_5y_comparison     — true sur la ligne de l'année la plus récente
 
    L'année de référence (−5 ans) est déterminée dynamiquement : si
    les données couvrent [2019, 2020, 2021, 2022, 2023, 2024] et que
    l'année la plus récente est 2024, la comparaison est 2024 vs 2019.
    """
    permission_classes = [IsAuthenticated]
 
    def get(self, request):
        company = request.user.company
 
        snapshots = (
            AgingSnapshot.objects
            .filter(company=company)
            .order_by("uploaded_at")
        )
 
        # ── 1. Grouper par année (garder le snapshot le plus récent) ──────────
        year_snapshots: dict = {}
        for snap in snapshots:
            if snap.aging_year:
                year = snap.aging_year
            elif snap.report_date:
                year = snap.report_date.year
            else:
                year = snap.uploaded_at.year
            if year not in year_snapshots or snap.uploaded_at > year_snapshots[year].uploaded_at:
                year_snapshots[year] = snap
 
        # ── 2. Compter les clients par année ──────────────────────────────────
        yearly_data = []
        for year in sorted(year_snapshots.keys()):
            snap  = year_snapshots[year]
            count = AgingReceivable.objects.filter(snapshot=snap, total__gt=0).count()
            yearly_data.append({
                "year":        year,
                "snapshot_id": str(snap.id),
                "label":       str(snap.report_date or snap.uploaded_at.date()),
                "customer_count": count,
            })
 
        if not yearly_data:
            return Response({"count": 0, "years": []})
 
        # ── 3. Fenêtre de 5 ans : année la plus récente vs (année − 5) ───────
        latest_year  = yearly_data[-1]["year"]
        ref_year     = latest_year - GROWTH_COMPARISON_YEARS
        # Chercher l'entrée la plus proche de ref_year (exacte si possible)
        ref_entry = next(
            (e for e in yearly_data if e["year"] == ref_year),
            None,
        )
        # Si l'année exacte n'est pas disponible, prendre la plus ancienne
        # dont l'écart avec latest_year est ≤ 5 ans
        if ref_entry is None and len(yearly_data) >= 2:
            candidates = [
                e for e in yearly_data[:-1]
                if latest_year - e["year"] <= GROWTH_COMPARISON_YEARS
            ]
            ref_entry = candidates[0] if candidates else yearly_data[0]
 
        ref_count = ref_entry["customer_count"] if ref_entry else None
        ref_year_actual = ref_entry["year"] if ref_entry else None
 
        # ── 4. Construire le résultat ligne par ligne ──────────────────────────
        result = []
        for i, entry in enumerate(yearly_data):
            prev       = yearly_data[i - 1] if i > 0 else None
            is_latest  = (entry["year"] == latest_year)
 
            # Taux de croissance annuel (inchangé)
            growth_rate     = None
            growth_absolute = None
            if prev and prev["customer_count"] > 0:
                growth_rate = round(
                    ((entry["customer_count"] - prev["customer_count"]) / prev["customer_count"]) * 100,
                    2,
                )
                growth_absolute = entry["customer_count"] - prev["customer_count"]
 
            # Taux vs 5 ans : affiché seulement sur la ligne la plus récente
            growth_rate_vs_5y    = None
            growth_abs_vs_5y     = None
            is_5y_comparison     = False
            if is_latest and ref_entry and ref_entry["year"] != latest_year and ref_count and ref_count > 0:
                growth_rate_vs_5y = round(
                    ((entry["customer_count"] - ref_count) / ref_count) * 100,
                    2,
                )
                growth_abs_vs_5y = entry["customer_count"] - ref_count
                is_5y_comparison = True
 
            result.append({
                "year":             entry["year"],
                "snapshot_id":      entry["snapshot_id"],
                "label":            entry["label"],
                "customer_count":   entry["customer_count"],
 
                # Comparaison N vs N-1 (tableau ligne par ligne)
                "prev_count":       prev["customer_count"] if prev else None,
                "growth_rate":      growth_rate,
                "growth_absolute":  growth_absolute,
                "is_growth":        (growth_rate > 0) if growth_rate is not None else None,
 
                # Comparaison N vs N-5 (badge principal)
                "growth_rate_vs_5y":   growth_rate_vs_5y,
                "growth_abs_vs_5y":    growth_abs_vs_5y,
                "is_5y_comparison":    is_5y_comparison,
                "ref_year_for_5y":     ref_year_actual if is_5y_comparison else None,
            })
 
        return Response({
            "count":                 len(result),
            "comparison_window":     GROWTH_COMPARISON_YEARS,
            "latest_year":           latest_year,
            "reference_year":        ref_year_actual,
            "reference_count":       ref_count,
            "years":                 result,
        })
 