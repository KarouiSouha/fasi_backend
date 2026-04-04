from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/auth/", include("apps.token_security.urls", namespace="token_security")),
    path("api/users/", include("apps.authentication.urls", namespace="authentication")),
    path("api/branches/", include("apps.branches.urls", namespace="branches")),
    path("api/companies/", include("apps.companies.urls", namespace="companies")),
    # ── Data Import (Excel upload) ─────────────────────────────────────
    path("api/import/", include("apps.data_import.urls", namespace="data_import")),
    # ── Products ──────────────────────────────────────────────────────
    path("api/products/", include("apps.products.urls", namespace="products")),
    # ── Customers ─────────────────────────────────────────────────────
    path("api/customers/", include("apps.customers.urls", namespace="customers")),
    # ── Inventory ─────────────────────────────────────────────────────
    path("api/inventory/", include("apps.inventory.urls", namespace="inventory")),
    # ── Transactions (Material Movements) ─────────────────────────────
    path("api/transactions/", include("apps.transactions.urls", namespace="transactions")),
    # ── Aging Receivables ─────────────────────────────────────────────
    path("api/aging/", include("apps.aging.urls", namespace="aging")),
    path("api/kpi/", include("apps.kpi.urls")),  # ADD THIS LINE
    # ── AI Insights ───────────────────────────────────────────────────
    path("api/ai-insights/", include("apps.ai_insights.urls")),
    path("api/notifications/", include("apps.notifications.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)