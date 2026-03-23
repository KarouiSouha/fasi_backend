"""
apps/ai_insights/models.py
--------------------------
Deux modèles ORM :
  1. AlertResolution  — alertes marquées résolues, scopées par company.
  2. AIUsageLog       — consommation de tokens AI pour monitoring des coûts.
"""

import uuid
from django.conf import settings
from django.db import models


class AlertResolution(models.Model):
    """
    Tracks which auto-generated alerts a company has marked as resolved.

    Alert IDs are deterministic strings produced by the frontend:
        "combined-<uuid>"   ← merged overdue+risk alert
        "old-overdue-<uuid>"
        "zero-stock-EC0020"
        "sales-drop-2025-11"

    Deleting a row re-opens the alert for the company.
    """

    ALERT_TYPE_CHOICES = [
        ("overdue",          "Overdue Payment"),
        ("risk",             "Credit Risk"),
        ("low_stock",        "Low Stock"),
        ("sales_drop",       "Sales Drop"),
        ("high_receivables", "High Receivables"),
        ("churn",            "Churn Risk"),
        ("dso",              "DSO Alert"),
        ("concentration",    "Client Concentration Risk"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        related_name="alert_resolutions",
        verbose_name="Company",
    )

    alert_id = models.CharField(max_length=255, verbose_name="Alert ID", db_index=True)

    alert_type = models.CharField(
        max_length=50, choices=ALERT_TYPE_CHOICES, verbose_name="Alert Type"
    )

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="resolved_alerts",
        verbose_name="Resolved By",
    )

    resolved_at = models.DateTimeField(auto_now_add=True, verbose_name="Resolved At")

    notes = models.TextField(
        blank=True, default="",
        verbose_name="Resolution Notes",
        help_text="Optional comment added by the agent when resolving.",
    )

    class Meta:
        db_table           = "ai_alert_resolution"
        verbose_name       = "Alert Resolution"
        verbose_name_plural= "Alert Resolutions"
        ordering           = ["-resolved_at"]
        unique_together    = [("company", "alert_id")]

    def __str__(self):
        name = self.resolved_by.get_full_name() if self.resolved_by else "unknown"
        return f"[{self.alert_type}] {self.alert_id} — resolved by {name}"


class AIUsageLog(models.Model):
    """
    Logs every AI API call for cost monitoring and transparency.

    Allows building a dashboard showing:
      - Total tokens consumed per company per day
      - Estimated cost in USD
      - Success rate (AI vs rule-based fallback)
      - Breakdown by analyzer (churn, risk_alert, hv_churn)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        related_name="ai_usage_logs",
        verbose_name="Company",
        null=True, blank=True,
    )

    analyzer = models.CharField(
        max_length=50,
        verbose_name="Analyzer",
        help_text="e.g. churn_predictor, risk_alert, hv_churn_outcome",
        db_index=True,
    )

    model = models.CharField(
        max_length=100,
        verbose_name="AI Model",
        help_text="e.g. gpt-4o-mini, claude-haiku-4-5-20251001",
    )

    tokens_used = models.IntegerField(default=0, verbose_name="Tokens Used")

    cost_usd = models.DecimalField(
        max_digits=10, decimal_places=8,
        default=0,
        verbose_name="Estimated Cost (USD)",
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Timestamp", db_index=True)

    class Meta:
        db_table           = "ai_usage_log"
        verbose_name       = "AI Usage Log"
        verbose_name_plural= "AI Usage Logs"
        ordering           = ["-created_at"]

    def __str__(self):
        return f"[{self.analyzer}] {self.tokens_used} tokens — ${self.cost_usd}"


class AIConversation(models.Model):
    """Persistent chat session for the Decision Advisor."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        related_name="ai_conversations",
        verbose_name="Company",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_conversations",
        verbose_name="User",
    )

    title = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created at")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Updated at", db_index=True)

    class Meta:
        db_table = "ai_conversation"
        verbose_name = "AI Conversation"
        verbose_name_plural = "AI Conversations"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Conversation {self.id} ({self.user.email})"


class AIConversationMessage(models.Model):
    """One message in a persistent Decision Advisor conversation."""

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    conversation = models.ForeignKey(
        AIConversation,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name="Conversation",
    )

    role = models.CharField(max_length=20, choices=Role.choices)
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_conversation_message"
        verbose_name = "AI Conversation Message"
        verbose_name_plural = "AI Conversation Messages"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role} @ {self.created_at.isoformat()}"