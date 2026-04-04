from rest_framework import serializers
from .models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            'id', 'alert_type', 'severity', 'title', 'message',
            'detail', 'metadata', 'is_read', 'read_at', 'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'read_at']


class AlertSyncItemSerializer(serializers.Serializer):
    frontend_id = serializers.CharField(max_length=255)
    alert_type = serializers.ChoiceField(choices=[
        'low_stock', 'overdue', 'risk', 'sales_drop', 'high_receivables',
        'dso', 'concentration', 'churn', 'anomaly', 'scheduled_report', 'system',
    ])
    severity = serializers.ChoiceField(choices=['low', 'medium', 'critical'])
    title = serializers.CharField(max_length=255)
    message = serializers.CharField()
    detail = serializers.CharField(required=False, default='', allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict)


class MarkReadSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list
    )
    all = serializers.BooleanField(required=False, default=False)