from django.utils import timezone
from django.db import IntegrityError
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .models import Notification
from .serializers import (
    NotificationSerializer,
    AlertSyncItemSerializer,
    MarkReadSerializer,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper : company_id basé sur l'utilisateur connecté
# ─────────────────────────────────────────────────────────────────────────────

def get_company_id(request):
    """
    Utilise l'ID de l'utilisateur connecté comme clé d'isolation.
    Chaque user ne voit que ses propres notifications.
    """
    return str(request.user.id)


# ─────────────────────────────────────────────────────────────────────────────
# Pagination
# ─────────────────────────────────────────────────────────────────────────────

class NotificationPagination(PageNumberPagination):
    page_size = 15
    page_size_query_param = 'page_size'
    max_page_size = 100


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/notifications/
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_notifications(request):
    company_id = get_company_id(request)
    qs = Notification.objects.filter(company_id=company_id)

    # Filtres optionnels
    severity   = request.query_params.get('severity')
    alert_type = request.query_params.get('alert_type')
    is_read    = request.query_params.get('is_read')
    search     = request.query_params.get('search', '').strip()

    if severity:
        qs = qs.filter(severity=severity)
    if alert_type:
        qs = qs.filter(alert_type=alert_type)
    if is_read is not None:
        qs = qs.filter(is_read=is_read.lower() in ('true', '1', 'yes'))
    if search:
        qs = qs.filter(
            Q(title__icontains=search) | Q(message__icontains=search)
        )

    paginator = NotificationPagination()
    page      = paginator.paginate_queryset(qs, request)
    serializer = NotificationSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
# DELETE /api/notifications/{id}/
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_notification(request, pk):
    company_id = get_company_id(request)
    try:
        notif = Notification.objects.get(id=pk, company_id=company_id)
        notif.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    except Notification.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/notifications/sync/
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sync_alerts(request):
    """
    Upsert des alertes depuis AlertsPage.
    Utilise update_or_create sur (company_id, frontend_id) — aucun doublon.
    """
    company_id = get_company_id(request)

    data = request.data
    if isinstance(data, dict):
        data = data.get('alerts', data)

    serializer = AlertSyncItemSerializer(data=data, many=True)
    if not serializer.is_valid():
        print("SYNC ERROR:", serializer.errors)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    created_count = 0
    updated_count = 0

    for item in serializer.validated_data:
        frontend_id = item['frontend_id']
        defaults = {
            'alert_type': item['alert_type'],
            'severity':   item['severity'],
            'title':      item['title'],
            'message':    item['message'],
            'detail':     item.get('detail', ''),
            'metadata':   item.get('metadata', {}),
        }
        try:
            _, created = Notification.objects.update_or_create(
                company_id=company_id,
                frontend_id=frontend_id,
                defaults=defaults,
            )
            if created:
                created_count += 1
            else:
                updated_count += 1
        except IntegrityError:
            # Race condition — déjà existant, on ignore
            pass

    return Response({
        'created': created_count,
        'updated': updated_count,
        'total':   created_count + updated_count,
    }, status=status.HTTP_200_OK)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/notifications/mark-read/
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_read(request):
    """
    Body : { "ids": ["uuid1", "uuid2"] }  → marque les IDs spécifiés
           { "all": true }                 → marque tout comme lu
    """
    company_id = get_company_id(request)
    serializer = MarkReadSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    now = timezone.now()

    if serializer.validated_data.get('all'):
        count = Notification.objects.filter(
            company_id=company_id, is_read=False
        ).update(is_read=True, read_at=now)
        return Response({'marked': count})

    ids = serializer.validated_data.get('ids', [])
    if ids:
        count = Notification.objects.filter(
            id__in=ids, company_id=company_id, is_read=False
        ).update(is_read=True, read_at=now)
        return Response({'marked': count})

    return Response({'marked': 0})