from django.urls import path
from . import views

urlpatterns = [
    # Liste + filtres + pagination
    path('', views.list_notifications, name='notifications-list'),

    # Détection côté backend (appelée au 1er chargement dashboard)
    path('detect/', views.detect_notifications, name='notifications-detect'),

    # Sync depuis AlertsPage (upsert)
    path('sync/', views.sync_alerts, name='notifications-sync'),

    # Marquer lu (une ou toutes)
    path('mark-read/', views.mark_read, name='notifications-mark-read'),

    # Supprimer une notification
    path('<uuid:pk>/', views.delete_notification, name='notifications-delete'),
]