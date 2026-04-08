from django.urls import path
from .views import (
    TransactionListView,
    TransactionDetailView,
    TransactionSummaryView,
    TransactionTypeBreakdownView,
    TransactionBranchBreakdownView,
    TransactionMovementTypesView,
    TransactionBranchesView,
    TransactionBranchMonthlyView,
    TransactionYearsView,
)

app_name = "transactions"

urlpatterns = [
    # GET /api/transactions/                    → paginated list + filters
    path("", TransactionListView.as_view(), name="transaction-list"),

    # GET /api/transactions/movement-types/     → distinct Arabic movement type labels
    path("movement-types/", TransactionMovementTypesView.as_view(), name="movement-types"),

    # GET /api/transactions/branches/           → distinct branch names
    path("branches/", TransactionBranchesView.as_view(), name="branches"),

    # GET /api/transactions/summary/            → monthly sales vs purchases
    path("summary/", TransactionSummaryView.as_view(), name="transaction-summary"),

    # GET /api/transactions/type-breakdown/     → totals grouped by movement type
    path("type-breakdown/", TransactionTypeBreakdownView.as_view(), name="type-breakdown"),

    # GET /api/transactions/branch-breakdown/   → sales totals grouped by branch
    path("branch-breakdown/", TransactionBranchBreakdownView.as_view(),name="branch-breakdown"),
    
    path("branch-monthly/", TransactionBranchMonthlyView.as_view(), name="branch-monthly"),  # ← new
    path("years/", TransactionYearsView.as_view(), name="years"),

    # GET /api/transactions/{id}/               → full movement detail
    path("<uuid:movement_id>/", TransactionDetailView.as_view(), name="transaction-detail"),
]