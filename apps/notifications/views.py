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


def _to_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def detect_notifications_for_user(request):
    """
    Lightweight server-side detection so mobile doesn't depend on the web
    Alerts page to populate notifications.
    """
    user = request.user
    company_id = get_company_id(request)
    company = getattr(user, 'company', None)

    if not company:
        return

    created_or_updated = 0

    # Deferred imports avoid cross-app import side effects at startup.
    from apps.aging.models import AgingSnapshot, AgingReceivable
    from apps.inventory.models import InventorySnapshotLine

    latest_aging = AgingSnapshot.objects.filter(company=company).order_by('-uploaded_at').first()
    if latest_aging:
        top_risk = (
            AgingReceivable.objects
            .filter(snapshot=latest_aging)
            .order_by('-total')[:50]
        )

        for row in top_risk:
            overdue = _to_float(row.overdue_total)
            total = _to_float(row.total)
            if overdue <= 0 or total <= 0:
                continue

            ratio = overdue / total
            if ratio < 0.5:
                continue

            severity = 'critical' if ratio >= 0.75 else 'medium'
            customer_name = row.customer_name or row.account
            frontend_id = f"aging-risk:{latest_aging.id}:{row.id}"

            _, was_created = Notification.objects.update_or_create(
                company_id=company_id,
                frontend_id=frontend_id,
                defaults={
                    'alert_type': 'risk',
                    'severity': severity,
                    'title': f"Credit risk for {customer_name}",
                    'message': f"Overdue receivables reached {overdue:,.2f} (ratio {(ratio * 100):.0f}%).",
                    'detail': f"Account {row.account_code} • Total exposure {total:,.2f}",
                    'metadata': {
                        'source': 'server_detect',
                        'snapshot_id': str(latest_aging.id),
                        'aging_record_id': str(row.id),
                        'account_code': row.account_code,
                        'customer_name': row.customer_name,
                        'overdue_total': overdue,
                        'total': total,
                        'overdue_ratio': ratio,
                    },
                },
            )
            if was_created:
                created_or_updated += 1

    latest_inventory_date = (
        InventorySnapshotLine.objects
        .filter(company=company)
        .order_by('-uploaded_at')
        .values_list('uploaded_at', flat=True)
        .first()
    )
    if latest_inventory_date:
        low_stock_lines = (
            InventorySnapshotLine.objects
            .filter(company=company, quantity__lte=0)
            .order_by('line_value')[:20]
        )

        for line in low_stock_lines:
            frontend_id = f"low-stock:{company_id}:{line.product_code}:{line.branch_name}"
            _, was_created = Notification.objects.update_or_create(
                company_id=company_id,
                frontend_id=frontend_id,
                defaults={
                    'alert_type': 'low_stock',
                    'severity': 'critical',
                    'title': f"Stockout risk: {line.product_name or line.product_code}",
                    'message': f"{line.branch_name}: quantity is {line.quantity} for {line.product_code}.",
                    'detail': f"Latest inventory import: {latest_inventory_date:%Y-%m-%d}",
                    'metadata': {
                        'source': 'server_detect',
                        'snapshot_id': '00000000-0000-0000-0000-000000000001',
                        'product_code': line.product_code,
                        'product_name': line.product_name,
                        'branch_name': line.branch_name,
                        'quantity': _to_float(line.quantity),
                    },
                },
            )
            if was_created:
                created_or_updated += 1

    return created_or_updated


# ─────────────────────────────────────────────────────────────────────────────
# Helper : company_id basé sur l'utilisateur connecté
# ─────────────────────────────────────────────────────────────────────────────

def get_company_id(request):
    """
    Uses the authenticated user's ID as the isolation key.
    Each user can only view their own notifications.
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
    auto_detect = request.query_params.get('auto_detect', 'true').lower() in ('true', '1', 'yes')
    if auto_detect:
        try:
            detect_notifications_for_user(request)
        except Exception:
            # Detection is best-effort and should never block listing.
            pass

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
# POST /api/notifications/detect/
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def detect_notifications(request):
    """
    Triggers best-effort server-side detection immediately.
    Useful when entering Dashboard so bells/badges can refresh without waiting
    for the Alerts page sync flow.
    """
    created = 0
    try:
        created = detect_notifications_for_user(request) or 0
    except Exception:
        # Detection should never crash the request pipeline.
        created = 0

    return Response({'created': created}, status=status.HTTP_200_OK)


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
    Upserts alerts from AlertsPage.

    Uses update_or_create with (company_id, frontend_id) as the unique key,
    ensuring that no duplicate records are created.
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
    Body:
        { "ids": ["uuid1", "uuid2"] }  → marks the specified notifications as read
        { "all": true }                → marks all notifications as read
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