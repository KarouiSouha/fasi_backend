"""
apps/ai_insights/urls.py
-------------------------
All Intelligent Analysis API endpoints.
"""
from django.urls import path
from .views import (
    # Alerts (SCRUM-29)
    AlertExplainView,
    AlertResolveView,
    AlertResolutionsView,
    KPIAnalysisView,
    AnomalyDetectionView,
    SeasonalAnalysisView,
    ChurnPredictionView,
    HighValueChurnView,
    StockOptimizationView,
    PredictionView,
    CriticalDetectionView,
    AIUsageView,    
)
from .chat_views import AIChatView, AIConversationListCreateView, AIConversationMessagesView
app_name = "ai_insights"

urlpatterns = [
    # ── Alerts ────────────────────────────────────────────────────────────────
    path("alerts/explain/",                AlertExplainView.as_view(),      name="alert-explain"),
    path("alerts/resolve/",                AlertResolveView.as_view(),      name="alert-resolve"),
    path("alerts/resolve/<str:alert_id>/", AlertResolveView.as_view(),      name="alert-resolve-delete"),
    path("alerts/resolutions/",            AlertResolutionsView.as_view(),  name="alert-resolutions"),
    path("kpis/",                          KPIAnalysisView.as_view(),       name="kpi-analysis"),
    path("anomalies/",                     AnomalyDetectionView.as_view(),  name="anomaly-detection"),
    path("seasonal/",                      SeasonalAnalysisView.as_view(),  name="seasonal-analysis"),
    path("churn/",                         ChurnPredictionView.as_view(),   name="churn-prediction"),
    path("churn/high-value/",              HighValueChurnView.as_view(),    name="hv-churn"),
    path("stock/",                         StockOptimizationView.as_view(), name="stock-optimization"),
    path("predict/",                       PredictionView.as_view(),        name="predict"),
    path("critical/",                      CriticalDetectionView.as_view(), name="critical-detection"),
    path("usage/",                         AIUsageView.as_view(),           name="ai-usage"),
    path("conversations/",                AIConversationListCreateView.as_view(), name="ai-conversations"),
    path("conversations/<uuid:conversation_id>/messages/", AIConversationMessagesView.as_view(), name="ai-conversation-messages"),
    path("chat/", AIChatView.as_view(), name="ai-chat"),
    
]