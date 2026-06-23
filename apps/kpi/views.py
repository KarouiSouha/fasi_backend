"""
apps/kpi/views.py

KPI Engine — Credit & Customer KPIs
Calculates all client/credit KPIs from aging and transactions data.

Endpoints:
    GET /api/kpi/credit/     → All 5 credit KPIs + top 5 risky customers
    GET /api/kpi/credit/summary/  → Lightweight version for dashboard
"""

import logging
from decimal import Decimal
from django.db.models import Sum, Count, Q, F, Case, When, Value, DecimalField
from django.db.models.functions import Coalesce
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# ── Bucket midpoints for DMP calculation (in days) ──────────────────────────
BUCKET_MIDPOINTS = {
    "current":   0,
    "d1_30":    15,
    "d31_60":   45,
    "d61_90":   75,
    "d91_120":  105,
    "d121_150": 135,
    "d151_180": 165,
    "d181_210": 195,
    "d211_240": 225,
    "d241_270": 255,
    "d271_300": 285,
    "d301_330": 315,
    "over_330": 360,
}

# Keywords that identify CASH (non-credit) transactions
CASH_KEYWORDS = ["نقدي", "قطاعي"]


class CreditKPIView(APIView):
    """
    GET /api/kpi/credit/

    Returns all 5 credit KPIs + top 5 risky customers.

    Query params:
        report_date=YYYY-MM-DD  — ignored (kept for backward compatibility)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.aging.models import AgingReceivable, AgingSnapshot
        from apps.customers.models import Customer
        from apps.transactions.models import MaterialMovement

        company = request.user.company
        if not company:
            return Response(
                {"error": "No company linked to this account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        snapshot_id_param = request.query_params.get("snapshot_id", "").strip()
        report_date_param = request.query_params.get("report_date", "").strip()
        aging_year_param = request.query_params.get("aging_year", "").strip()

        latest_snap = None
        if snapshot_id_param:
            try:
                latest_snap = AgingSnapshot.objects.get(id=snapshot_id_param, company=company)
            except AgingSnapshot.DoesNotExist:
                latest_snap = None
        elif aging_year_param:
            try:
                year = int(aging_year_param)
                latest_snap = (
                    AgingSnapshot.objects
                    .filter(company=company, aging_year=year)
                    .order_by("-uploaded_at")
                    .first()
                )
            except (TypeError, ValueError):
                latest_snap = None
        elif report_date_param:
            latest_snap = (
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
            latest_snap = AgingSnapshot.objects.filter(company=company).order_by("-uploaded_at").first()

        aging_qs = (
            AgingReceivable.objects.filter(snapshot=latest_snap)
            if latest_snap else AgingReceivable.objects.none()
        )

        # Exclude pure cash accounts (account_code 1141001 = قطاعي / نقدي)
        credit_aging_qs = aging_qs.exclude(
            Q(account__icontains="نقدي") | Q(account_code="1141001")
        )

        # ── 1. Taux de clients à crédit ──────────────────────────────────────
        # = (Customers with aging balance > 0) / (Total active customers) × 100
        total_customers = aging_qs.count() 
        credit_customers_count = aging_qs.filter(total__gt=0).count() 

        credit_customers_count = credit_aging_qs.filter(
            total__gt=0
        ).values("account_code").distinct().count()

        taux_clients_credit = (
            round((credit_customers_count / total_customers) * 100, 2)
            if total_customers > 0 else 0.0
        )

        # ── 2. Taux de crédit total ───────────────────────────────────────────
        # = (CA à crédit / CA total) × 100
        # CA total = SUM(total_out) for all sales
        # CA à crédit = SUM(total_out) for sales to named, non-cash customers
        SALE_LABELS = ["ف بيع"]

        sales_qs = MaterialMovement.objects.filter(
            company=company,
            movement_type__in=SALE_LABELS,
        )

        SALE_LABELS = ["ف بيع"]
        CASH_KEYWORDS_FILTER = Q(customer_name__icontains="نقدي") | Q(customer_name__icontains="قطاعي")

        sales_qs = MaterialMovement.objects.filter(
            company=company,
            movement_type__in=SALE_LABELS,
        )

        ca_total_agg = sales_qs.aggregate(ca=Coalesce(Sum("total_out"), Decimal("0")))
        ca_total = float(ca_total_agg["ca"])

        ca_credit_agg = sales_qs.exclude(CASH_KEYWORDS_FILTER).exclude(
            Q(customer_name__isnull=True) | Q(customer_name="")
        ).aggregate(ca=Coalesce(Sum("total_out"), Decimal("0")))
        ca_credit = float(ca_credit_agg["ca"])

        # ── 3. Taux d'impayés ─────────────────────────────────────────────────
        # = (Montant impayé / Montant total à recouvrer) × 100
        # Impayé = everything overdue (d61_90 and beyond)
        aging_totals = aging_qs.aggregate(
            grand_total=Coalesce(Sum("total"), Decimal("0")),
            sum_current=Coalesce(Sum("current"), Decimal("0")),
            sum_d1_30=Coalesce(Sum("d1_30"), Decimal("0")),
            sum_d31_60=Coalesce(Sum("d31_60"), Decimal("0")),
            sum_d61_90=Coalesce(Sum("d61_90"), Decimal("0")),
            sum_d91_120=Coalesce(Sum("d91_120"), Decimal("0")),
            sum_d121_150=Coalesce(Sum("d121_150"), Decimal("0")),
            sum_d151_180=Coalesce(Sum("d151_180"), Decimal("0")),
            sum_d181_210=Coalesce(Sum("d181_210"), Decimal("0")),
            sum_d211_240=Coalesce(Sum("d211_240"), Decimal("0")),
            sum_d241_270=Coalesce(Sum("d241_270"), Decimal("0")),
            sum_d271_300=Coalesce(Sum("d271_300"), Decimal("0")),
            sum_d301_330=Coalesce(Sum("d301_330"), Decimal("0")),
            sum_over_330=Coalesce(Sum("over_330"), Decimal("0")),
        )

        grand_total = float(aging_totals["grand_total"])
        credit_realized = ca_total - grand_total
        if ca_total > 0:
         taux_credit_total = (credit_realized / ca_total) * 100
        else:
          taux_credit_total = 0.0
        # Overdue = d61_90 and beyond (past 60 days is truly overdue)
        overdue = sum(float(aging_totals[f"sum_{b}"]) for b in [
            "d61_90", "d91_120", "d121_150", "d151_180",
            "d181_210", "d211_240", "d241_270", "d271_300",
            "d301_330", "over_330"
        ])

        taux_impayes = (
            round((overdue / grand_total) * 100, 2)
            if grand_total > 0 else 0.0
        )

        # ── 4. Délai moyen de paiement (DMP) ──────────────────────────────────
        # DMP = Σ(bucket_midpoint × bucket_amount) / total_credit_amount
        # Uses weighted average of aging bucket midpoints
        bucket_values = {
            "current":   float(aging_totals["sum_current"]),
            "d1_30":     float(aging_totals["sum_d1_30"]),
            "d31_60":    float(aging_totals["sum_d31_60"]),
            "d61_90":    float(aging_totals["sum_d61_90"]),
            "d91_120":   float(aging_totals["sum_d91_120"]),
            "d121_150":  float(aging_totals["sum_d121_150"]),
            "d151_180":  float(aging_totals["sum_d151_180"]),
            "d181_210":  float(aging_totals["sum_d181_210"]),
            "d211_240":  float(aging_totals["sum_d211_240"]),
            "d241_270":  float(aging_totals["sum_d241_270"]),
            "d271_300":  float(aging_totals["sum_d271_300"]),
            "d301_330":  float(aging_totals["sum_d301_330"]),
            "over_330":  float(aging_totals["sum_over_330"]),
        }

        weighted_sum = sum(
            BUCKET_MIDPOINTS[bucket] * amount
            for bucket, amount in bucket_values.items()
        )

        dmp = round(weighted_sum / grand_total, 1) if grand_total > 0 else 0.0

        # ── 5. Taux de recouvrement ───────────────────────────────────────────
        # = (Montant récupéré / Montant total à recouvrer) × 100
        # Montant récupéré = CA crédit - Aging total (what's still owed)
        # Represents portion of credit sales already paid
        montant_recupere = max(0.0, ca_credit - grand_total)
        taux_recouvrement = (
            round((montant_recupere / ca_credit) * 100, 2)
            if ca_credit > 0 else 0.0
        )
        base_recouvrement = ca_total - grand_total
        montant_recupere = max(0.0, ca_credit - grand_total)
        taux_recouvrement = (
        round((montant_recupere / base_recouvrement) * 100, 2)
        if base_recouvrement > 0 else 0.0
      )
        # ── Top 5 risky customers ─────────────────────────────────────────────
        all_credit_records = list(
            credit_aging_qs.filter(0)
            .select_related("customer")
            .order_by("-total")
        )

        # Sort by risk: critical > high > medium > low, then by overdue_total
        risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        all_credit_records.sort(
            key=lambda r: (risk_order.get(r.risk_score, 4), -float(r.total))
        )
        top5 = all_credit_records[:5]

        top5_data = [
            {
                "id": str(r.id),
                "account": r.account,
                "account_code": r.account_code,
                "customer_name": r.customer.name if r.customer else _extract_name(r.account),
                "total": float(r.total),
                "current": float(r.current),
                "overdue_total": float(r.overdue_total),
                "risk_score": r.risk_score,
                "overdue_percentage": round((float(r.overdue_total) / float(r.total)) * 100, 1)
                    if float(r.total) > 0 else 0.0,
                "dmp_days": _calc_record_dmp(r),
                "buckets": {
                    "current":    float(r.current),
                    "d1_30":      float(r.d1_30),
                    "d31_60":     float(r.d31_60),
                    "d61_90":     float(r.d61_90),
                    "d91_120":    float(r.d91_120),
                    "d121_150":   float(r.d121_150),
                    "d151_180":   float(r.d151_180),
                    "d181_210":   float(r.d181_210),
                    "d211_240":   float(r.d211_240),
                    "d241_270":   float(r.d241_270),
                    "d271_300":   float(r.d271_300),
                    "d301_330":   float(r.d301_330),
                    "over_330":   float(r.over_330),
                },
            }
            for r in top5
        ]

        # ── Aging distribution by bucket (for chart) ─────────────────────────
        bucket_distribution = [
            {
                "bucket": bucket,
                "label": label,
                "amount": bucket_values.get(bucket, 0.0),
                "percentage": round(bucket_values.get(bucket, 0.0) / grand_total * 100, 1)
                    if grand_total > 0 else 0.0,
                "midpoint_days": BUCKET_MIDPOINTS[bucket],
            }
            for bucket, label in [
                ("current",   "Current"),
                ("d1_30",     "1-30d"),
                ("d31_60",    "31-60d"),
                ("d61_90",    "61-90d"),
                ("d91_120",   "91-120d"),
                ("d121_150",  "121-150d"),
                ("d151_180",  "151-180d"),
                ("d181_210",  "181-210d"),
                ("d211_240",  "211-240d"),
                ("d241_270",  "241-270d"),
                ("d271_300",  "271-300d"),
                ("d301_330",  "301-330d"),
                ("over_330",  ">330d"),
            ]
        ]

        return Response({
            "report_date": None,
            "kpis": {
                "taux_clients_credit": {
                    "value": taux_clients_credit,
                    "numerator": credit_customers_count,
                    "denominator": total_customers,
                    "label": "Credit Customer Rate",
                    "unit": "%",
                    "description": "Share of customers with an active credit balance",
                },
                "taux_credit_total": {
                    "value": taux_credit_total,
                    "ca_credit": round(ca_credit, 2),
                    "ca_total": round(ca_total, 2),
                    "label": "Total Credit Rate",
                    "unit": "%",
                    "description": "Share of revenue realized on credit terms",
                },
                "taux_impayes": {
                    "value": taux_impayes,
                    "overdue_amount": round(overdue, 2),
                    "total_receivables": round(grand_total, 2),
                    "label": "Overdue Rate",
                    "unit": "%",
                    "description": "Overdue receivables as a percentage of total receivables",
                },
                "dmp": {
                    "value": dmp,
                    "label": "DSO (Avg. Payment Days)",
                    "unit": "days",
                    "description": "Average number of days customers take to pay",
                },
                "taux_recouvrement": {
                    "value": taux_recouvrement,
                    "recovered_amount": round(montant_recupere, 2),
                    "total_credit": round(ca_total - grand_total, 2),
                    "label": "Collection Rate",
                    "unit": "%",
                    "description": "Percentage of credit sales successfully collected",
                },
            },
            "top5_risky_customers": top5_data,
            "bucket_distribution": bucket_distribution,
            "summary": {
                "total_customers": total_customers,
                "credit_customers": credit_customers_count,
                "grand_total_receivables": round(grand_total, 2),
                "overdue_amount": round(overdue, 2),
                "ca_credit": round(ca_credit, 2),
                "ca_total": round(ca_total, 2),
            },
        })


def _extract_name(account_str: str) -> str:
    """Extract customer name from account string like '1141001 - اسم العميل'"""
    if not account_str:
        return ""
    parts = account_str.split("-", 1)
    return parts[1].strip() if len(parts) > 1 else account_str.strip()


def _calc_record_dmp(record) -> float:
    """Calculate DMP for a single aging record."""
    total = float(record.total)
    if total <= 0:
        return 0.0
    buckets = {
        "current":  float(record.current),
        "d1_30":    float(record.d1_30),
        "d31_60":   float(record.d31_60),
        "d61_90":   float(record.d61_90),
        "d91_120":  float(record.d91_120),
        "d121_150": float(record.d121_150),
        "d151_180": float(record.d151_180),
        "d181_210": float(record.d181_210),
        "d211_240": float(record.d211_240),
        "d241_270": float(record.d241_270),
        "d271_300": float(record.d271_300),
        "d301_330": float(record.d301_330),
        "over_330": float(record.over_330),
    }
    weighted = sum(BUCKET_MIDPOINTS[b] * v for b, v in buckets.items())
    return round(weighted / total, 1)