"""
apps/aging/serializers.py
-------------------------
Serializers for the AgingReceivable model.

Field names match exactly what the frontend TypeScript AgingRow interface expects.
The AgingListView returns { records: [...] } and the dataHooks.ts useAgingReport
hook maps that key to { results: [...] } automatically.
"""
from rest_framework import serializers
from .models import AgingReceivable, AgingSnapshot


# ── Full record (detail view) ─────────────────────────────────────────────────

class AgingReceivableSerializer(serializers.ModelSerializer):
    """
    Full serializer: all buckets + computed risk_score / overdue_total.
    Used by AgingDetailView → GET /api/aging/<id>/
    """

    risk_score    = serializers.SerializerMethodField()
    overdue_total = serializers.SerializerMethodField()
    customer_name = serializers.CharField(
        source="customer.name",
        read_only=True,
        allow_null=True,
        default=None,
    )

    class Meta:
        model  = AgingReceivable
        fields = [
            "id",
            "customer", "customer_name",
            "account", "account_code",
            # 13 aging buckets
            "current",
            "d1_30", "d31_60", "d61_90", "d91_120",
            "d121_150", "d151_180", "d181_210", "d211_240",
            "d241_270", "d271_300", "d301_330", "over_330",
            # Computed
            "total", "overdue_total", "risk_score",
            # Timestamps
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_risk_score(self, obj) -> str:
        return obj.risk_score        # @property on AgingReceivable model

    def get_overdue_total(self, obj) -> float:
        return float(obj.overdue_total)  # @property on AgingReceivable model


# ── List / report view ────────────────────────────────────────────────────────

class AgingListSerializer(serializers.ModelSerializer):
    """
    Serializer for paginated list, KPI credit, and risk views.
    Used by:
      - AgingListView      → GET /api/aging/
      - AgingRiskView      → GET /api/aging/risk/
      - CreditKPIView      → GET /api/kpi/credit/

    Returns ALL 13 bucket columns so that:
      - AgingReceivablePage renders the full bucket table
      - CreditKPISection renders the per-customer mini histogram
    """

    risk_score    = serializers.SerializerMethodField()
    overdue_total = serializers.SerializerMethodField()
    customer_name = serializers.CharField(
        source="customer.name",
        read_only=True,
        allow_null=True,
        default=None,
    )

    snapshot_id = serializers.UUIDField(source='snapshot.id', read_only=True)

    class Meta:
        model  = AgingReceivable
        fields = [
            "id", "snapshot_id",
            "account_code", "account", "customer_name",
            # All 13 buckets
            "current",
            "d1_30", "d31_60", "d61_90", "d91_120",
            "d121_150", "d151_180", "d181_210", "d211_240",
            "d241_270", "d271_300", "d301_330", "over_330",
            # Computed
            "total", "overdue_total", "risk_score",
        ]
        read_only_fields = fields

    def get_risk_score(self, obj) -> str:
        return obj.risk_score

    def get_overdue_total(self, obj) -> float:
        return float(obj.overdue_total)


# ── Snapshot list ─────────────────────────────────────────────────────────────

class AgingSnapshotSerializer(serializers.ModelSerializer):
    """
    Summary representation of one aging import session.
    Used by AgingSnapshotListView → GET/DELETE /api/aging/snapshots/
    """
    uploaded_by_name = serializers.SerializerMethodField()
    line_count       = serializers.SerializerMethodField()
    grand_total      = serializers.SerializerMethodField()

    class Meta:
        model  = AgingSnapshot
        fields = [
            "id",
            "aging_year",
            "report_date",
            "source_file",
            "uploaded_by_name",
            "uploaded_at",
            "line_count",
            "grand_total",
        ]
        read_only_fields = fields

    def get_uploaded_by_name(self, obj) -> str:
        if not obj.uploaded_by:
            return None
        return obj.uploaded_by.get_full_name() or obj.uploaded_by.username

    def get_line_count(self, obj) -> int:
        return obj.lines.count()

    def get_grand_total(self, obj) -> float:
        from django.db.models import Sum
        result = obj.lines.aggregate(t=Sum('total'))['t']
        return float(result or 0)


# ── Distribution aggregation ──────────────────────────────────────────────────

class AgingDistributionSerializer(serializers.Serializer):
    """
    One row of the bucket distribution response.
    GET /api/aging/distribution/
    → { bucket: "d61_90", label: "61–90 days", total: 45230.50, percentage: 12.3 }
    """
    bucket     = serializers.CharField()
    label      = serializers.CharField()
    total      = serializers.FloatField()
    percentage = serializers.FloatField()


# ── Dates list ────────────────────────────────────────────────────────────────

class AgingDatesSerializer(serializers.Serializer):
    """
    Available import dates for the date-picker.
    GET /api/aging/dates/
    → { dates: ["2025-12-31", "2024-12-31"] }
    """
    dates = serializers.ListField(child=serializers.DateField())