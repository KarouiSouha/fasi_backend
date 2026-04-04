from django.db import models
from django.conf import settings
import uuid


class Notification(models.Model):
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('critical', 'Critical'),
    ]

    ALERT_TYPE_CHOICES = [
        ('low_stock', 'Low Stock'),
        ('overdue', 'Overdue Payment'),
        ('risk', 'Credit Risk'),
        ('sales_drop', 'Sales Drop'),
        ('high_receivables', 'High Receivables'),
        ('dso', 'DSO Alert'),
        ('concentration', 'Client Concentration'),
        ('churn', 'Churn'),
        ('anomaly', 'Anomaly'),
        ('scheduled_report', 'Scheduled Report'),
        ('system', 'System'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Lié à l'utilisateur si auth activée, sinon company-wide
    company_id = models.CharField(max_length=64, blank=True, default='default')

    # Clé logique pour éviter les doublons côté DB
    frontend_id = models.CharField(max_length=255, blank=True, db_index=True)

    alert_type = models.CharField(max_length=32, choices=ALERT_TYPE_CHOICES)
    severity = models.CharField(max_length=16, choices=SEVERITY_CHOICES, default='medium')

    title = models.CharField(max_length=255)
    message = models.TextField()
    detail = models.TextField(blank=True, default='')

    metadata = models.JSONField(default=dict, blank=True)

    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['company_id', 'is_read']),
            models.Index(fields=['company_id', 'alert_type']),
            models.Index(fields=['frontend_id']),
        ]
        # Contrainte d'unicité sur frontend_id pour éviter les doublons en DB
        constraints = [
            models.UniqueConstraint(
                fields=['company_id', 'frontend_id'],
                name='unique_notification_per_company',
                condition=~models.Q(frontend_id=''),
            )
        ]

    def __str__(self):
        return f"[{self.severity.upper()}] {self.title} ({self.created_at:%Y-%m-%d})"