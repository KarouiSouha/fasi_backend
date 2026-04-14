# apps/data_import/urls.py
from django.urls import path
from .views import (
    ExcelUploadView,
    DetectFileTypeView,
    ValidateExcelView,
    ImportLogListView,
    ImportLogDetailView,
)

app_name = "data_import"

urlpatterns = [
    # POST /api/import/upload/    → Upload et traitement du fichier Excel
    path('upload/', ExcelUploadView.as_view(), name='upload'),

    # POST /api/import/validate/  → Validation stricte du fichier Excel (sans import)
    path('validate/', ValidateExcelView.as_view(), name='validate'),

    # POST /api/import/detect/     → Détection du type de fichier + preview (sans validation)
    path('detect/', DetectFileTypeView.as_view(), name='detect'),

    # GET /api/import/logs/        → Liste des historiques d'import pour la société
    path('logs/', ImportLogListView.as_view(), name='logs-list'),

    # GET /api/import/logs/{id}/   → Détails d'un import spécifique
    # DELETE /api/import/logs/{id}/ → Suppression d'un log d'import
    path('logs/<int:log_id>/', ImportLogDetailView.as_view(), name='logs-detail'),
]