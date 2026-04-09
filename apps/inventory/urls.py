from django.urls import path
from .views import (
    InventoryListView,
    InventoryDetailView,
    InventorySnapshotLinesView,
    InventoryLinesView,
    InventoryBranchSummaryView,
    InventorySnapshotDatesView,
    InventoryCategoryBreakdownView,
)

app_name = "inventory"

urlpatterns = [
    # GET  /api/inventory/                           → list of snapshot sessions
    path("", InventoryListView.as_view(), name="inventory-list"),

    # GET  /api/inventory/lines/                     → paginated current stock lines
    path("lines/", InventoryLinesView.as_view(), name="inventory-lines-current"),

    # GET  /api/inventory/dates/                     → distinct import dates
    path("dates/", InventorySnapshotDatesView.as_view(), name="inventory-dates"),

    # GET  /api/inventory/branch-summary/            → totals per branch
    path("branch-summary/", InventoryBranchSummaryView.as_view(), name="branch-summary"),

    # GET  /api/inventory/category-breakdown/        → totals per product category
    path("category-breakdown/", InventoryCategoryBreakdownView.as_view(), name="category-breakdown"),

    # GET  /api/inventory/<uuid>/                    → snapshot metadata + branch list
    # DELETE /api/inventory/<uuid>/                  → delete snapshot and all its lines
    path("<uuid:snapshot_id>/", InventoryDetailView.as_view(), name="inventory-detail"),

    # GET  /api/inventory/<uuid>/lines/              → paginated product×branch lines
    path("<uuid:snapshot_id>/lines/", InventorySnapshotLinesView.as_view(), name="inventory-lines"),
]
