from django.urls import path
from .views import (
    AgingListView,
    AgingDetailView,
    AgingRiskView,
    AgingDistributionView,
    AgingReportDatesView,
    AgingSnapshotListView,
    AgingHistoricalTrendView,
    AgingCustomerGrowthView,
)

app_name = "aging"

urlpatterns = [
    # GET /api/aging/                           → paginated list
    path("", AgingListView.as_view(), name="aging-list"),

    # GET /api/aging/dates/                     → available report dates
    path("dates/", AgingReportDatesView.as_view(), name="aging-dates"),

    # GET /api/aging/risk/                      → top overdue customers by risk score
    path("risk/", AgingRiskView.as_view(), name="aging-risk"),

    # GET /api/aging/distribution/              → bucket totals for waterfall chart
    path("distribution/", AgingDistributionView.as_view(), name="aging-distribution"),

    # GET  /api/aging/snapshots/                → list all import snapshots
    # DELETE /api/aging/snapshots/<uuid>/       → roll back a snapshot
    path("snapshots/", AgingSnapshotListView.as_view(), name="aging-snapshot-list"),
    path("snapshots/<uuid:snapshot_id>/", AgingSnapshotListView.as_view(), name="aging-snapshot-detail"),

    # GET /api/aging/{id}/                      → full record detail
    path("<uuid:aging_id>/", AgingDetailView.as_view(), name="aging-detail"),
    path("historical-trend/", AgingHistoricalTrendView.as_view(), name="aging-historical-trend"),
    
    path("customer-growth/", AgingCustomerGrowthView.as_view(), name="aging-customer-growth"),

]
