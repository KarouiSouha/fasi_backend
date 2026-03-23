"""
apps/ai_insights/serializers.py
--------------------------------
DRF serializers for all AI Insights API responses.

These define the exact contract between the backend and the frontend.
The frontend TypeScript interfaces must mirror these schemas.
"""

from rest_framework import serializers
from .models import AlertResolution, AIConversation, AIConversationMessage


# ── Alert Resolution ──────────────────────────────────────────────────────────

class AlertResolutionSerializer(serializers.ModelSerializer):
    resolved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AlertResolution
        fields = [
            "id",
            "alert_id",
            "alert_type",
            "resolved_by",
            "resolved_by_name",
            "resolved_at",
            "notes",
        ]
        read_only_fields = fields

    def get_resolved_by_name(self, obj) -> str | None:
        if not obj.resolved_by:
            return None
        return obj.resolved_by.get_full_name() or obj.resolved_by.email


class AlertResolveInputSerializer(serializers.Serializer):
    alert_id   = serializers.CharField(max_length=255)
    alert_type = serializers.ChoiceField(
        choices=AlertResolution.ALERT_TYPE_CHOICES + [("dso", "DSO Alert"), ("concentration", "Client Concentration")]
    )
    notes      = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        default="",
    )


# ── Conversations ─────────────────────────────────────────────────────────────

class AIConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIConversation
        fields = ["id", "title", "created_at", "updated_at"]
        read_only_fields = fields


class AIConversationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIConversation
        fields = ["title"]


class AIConversationMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIConversationMessage
        fields = ["id", "role", "content", "metadata", "created_at"]
        read_only_fields = fields


# ── AI Explanation ────────────────────────────────────────────────────────────

class AlertExplainInputSerializer(serializers.Serializer):
    """Validates the request body for POST /ai-insights/alerts/explain/"""
    type     = serializers.CharField(max_length=50)
    severity = serializers.ChoiceField(choices=["low", "medium", "critical"])
    message  = serializers.CharField(max_length=500)
    detail   = serializers.CharField(max_length=500, required=False, allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict)


class AlertExplainResponseSerializer(serializers.Serializer):
    """Shape of the response from POST /ai-insights/alerts/explain/"""
    summary                  = serializers.CharField()
    root_cause               = serializers.CharField()
    urgency_reason           = serializers.CharField()
    recommended_actions      = serializers.ListField(child=serializers.CharField())
    risk_level_justification = serializers.CharField()
    confidence               = serializers.ChoiceField(choices=["high", "medium", "low"])
    cached                   = serializers.BooleanField()


# ── Churn Prediction ──────────────────────────────────────────────────────────

class ChurnPredictionItemSerializer(serializers.Serializer):
    """One customer's churn prediction."""
    customer_id               = serializers.CharField(allow_null=True)
    account_code              = serializers.CharField(allow_blank=True)
    customer_name             = serializers.CharField(allow_blank=True, default="")
    churn_score               = serializers.FloatField()
    churn_label               = serializers.ChoiceField(
        choices=["low", "medium", "high", "critical"]
    )
    days_since_last_purchase  = serializers.IntegerField()
    purchase_count_12m        = serializers.IntegerField()
    avg_monthly_revenue_lyd   = serializers.FloatField()
    avg_order_value_lyd       = serializers.FloatField()
    revenue_trend             = serializers.FloatField()
    aging_risk_score          = serializers.CharField()
    overdue_ratio             = serializers.FloatField()
    total_receivable_lyd      = serializers.FloatField()
    ai_explanation            = serializers.CharField()
    recommended_actions       = serializers.ListField(child=serializers.CharField())
    key_risk_factors          = serializers.ListField(child=serializers.CharField())
    confidence                = serializers.CharField()


class ChurnSummarySerializer(serializers.Serializer):
    total           = serializers.IntegerField()
    critical        = serializers.IntegerField()
    high            = serializers.IntegerField()
    medium          = serializers.IntegerField()
    low             = serializers.IntegerField()
    avg_churn_score = serializers.FloatField()


class ChurnPredictionResponseSerializer(serializers.Serializer):
    company_id  = serializers.CharField()
    top_n       = serializers.IntegerField()
    ai_used     = serializers.BooleanField()
    cached      = serializers.BooleanField()
    summary     = ChurnSummarySerializer()
    predictions = ChurnPredictionItemSerializer(many=True)