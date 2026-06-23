# apps/kpi/views_supply.py
# ══════════════════════════════════════════════════════════════════
# Supply Policy API  —  GET /api/kpi/supply/
# v5: Lead times now deduplicate by (supplier, date) — multiple
#     product lines on the same day = ONE order, not N.
#
# FIX 3: Branches resolved dynamically via FK (branch__name from
#        the Branch model).
#
# Query params:
#   year     (str)  — e.g. "2025" or "all"
#   branch   (str)  — English branch name (default: "all")
#   category (str)  — category value      (default: "all")
# ══════════════════════════════════════════════════════════════════

from collections import defaultdict
from datetime import datetime
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.transactions.models import MaterialMovement

def resolve_branch(fk_name: str | None) -> str:
    """
    Priority order:
    1. Branch FK name  (Branch.name — set at import time)
    2. 'Unknown'
    """
    fk  = (fk_name  or '.').strip()
    if fk:
        return fk
    return 'Unknown'


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def supply_kpi_view(request):
    year_param   = request.query_params.get('year',     'all')
    branch_param = request.query_params.get('branch',   'all')
    cat_param    = request.query_params.get('category', 'all')

    # ── 1. Base queryset: purchases only ─────────────────────────
    qs = MaterialMovement.objects.filter(movement_type='ف شراء')

    # ── 2. Single DB query ────────────────────────────────────────
    rows = qs.values(
        'movement_date',
        'branch__name',    # FK-resolved branch name
        'customer_name',   # supplier name
        'category',
        'material_name',
        'qty_in',
        'total_in',
    )

    # ── 3. Parse all rows ─────────────────────────────────────────
    all_years      = set()
    all_branches   = set()
    all_categories = set()
    parsed         = []

    for r in rows:
        date = r['movement_date']
        if hasattr(date, 'year'):
            y = str(date.year)
            m = date.strftime('%Y-%m')
        else:
            try:
                dt = datetime.fromisoformat(str(date))
                y  = str(dt.year)
                m  = dt.strftime('%Y-%m')
            except Exception:
                continue

        branch   = resolve_branch(r.get('branch__name'))
        supplier = (r.get('customer_name') or '').strip() or 'Unknown'
        category = (r.get('category')      or '').strip()
        material = (r.get('material_name') or '').strip()
        qty      = float(r.get('qty_in')   or 0)
        total    = float(r.get('total_in') or 0)

        all_years.add(y)
        all_branches.add(branch)
        if category:
            all_categories.add(category)

        parsed.append({
            'year': y, 'month': m,
            'branch': branch, 'supplier': supplier,
            'category': category, 'material': material,
            'qty': qty, 'total': total,
        })

    # ── 4. Apply filters ──────────────────────────────────────────
    filtered = parsed
    if year_param != 'all':
        filtered = [r for r in filtered if r['year'] == year_param]
    if branch_param != 'all':
        filtered = [r for r in filtered if r['branch'] == branch_param]
    if cat_param != 'all':
        filtered = [r for r in filtered if r['category'] == cat_param]

    # ── 5. Aggregate ──────────────────────────────────────────────

    # Meta KPIs
    total_value      = sum(r['total'] for r in filtered)
    total_qty        = sum(r['qty']   for r in filtered)
    unique_suppliers = len({r['supplier'] for r in filtered})
    unique_skus      = len({r['material'] for r in filtered})

    # Monthly
    monthly_map: dict = defaultdict(lambda: {'value': 0.0, 'qty': 0.0, 'count': 0})
    for r in filtered:
        monthly_map[r['month']]['value'] += r['total']
        monthly_map[r['month']]['qty']   += r['qty']
        monthly_map[r['month']]['count'] += 1
    monthly = [{'month': k, **v} for k, v in sorted(monthly_map.items())]

    # By branch
    branch_map: dict = defaultdict(lambda: {'value': 0.0, 'qty': 0.0, 'count': 0})
    for r in filtered:
        branch_map[r['branch']]['value'] += r['total']
        branch_map[r['branch']]['qty']   += r['qty']
        branch_map[r['branch']]['count'] += 1
    by_branch = sorted(
        [{'branch': k, **v} for k, v in branch_map.items()],
        key=lambda x: x['value'], reverse=True
    )

    # By supplier (top 20)
    sup_map: dict = defaultdict(lambda: {'value': 0.0, 'qty': 0.0, 'count': 0, 'skus': set()})
    for r in filtered:
        sup_map[r['supplier']]['value'] += r['total']
        sup_map[r['supplier']]['qty']   += r['qty']
        sup_map[r['supplier']]['count'] += 1
        sup_map[r['supplier']]['skus'].add(r['material'])
    by_supplier = sorted(
        [{'name': k, 'value': v['value'], 'qty': v['qty'],
          'count': v['count'], 'sku_count': len(v['skus'])}
         for k, v in sup_map.items()],
        key=lambda x: x['value'], reverse=True
    )[:20]

    # By category (top 20)
    cat_map: dict = defaultdict(lambda: {'value': 0.0, 'qty': 0.0, 'count': 0})
    for r in filtered:
        c = r['category'] or 'Other'
        cat_map[c]['value'] += r['total']
        cat_map[c]['qty']   += r['qty']
        cat_map[c]['count'] += 1
    by_category = sorted(
        [{'name': k, **v} for k, v in cat_map.items()],
        key=lambda x: x['value'], reverse=True
    )[:20]

    # Branch × Month
    bxm_map: dict = defaultdict(lambda: {'value': 0.0, 'qty': 0.0})
    for r in filtered:
        key = (r['branch'], r['month'])
        bxm_map[key]['value'] += r['total']
        bxm_map[key]['qty']   += r['qty']
    branch_month = sorted(
        [{'branch': k[0], 'month': k[1], **v} for k, v in bxm_map.items()],
        key=lambda x: (x['branch'], x['month'])
    )

    # Supplier × top SKUs (top 10 suppliers × top 5 SKUs each)
    top_sup_names = [s['name'] for s in by_supplier[:10]]
    sup_sku_map: dict[str, dict] = {
        n: defaultdict(lambda: {'value': 0.0, 'qty': 0.0})
        for n in top_sup_names
    }
    for r in filtered:
        if r['supplier'] in sup_sku_map:
            sup_sku_map[r['supplier']][r['material']]['value'] += r['total']
            sup_sku_map[r['supplier']][r['material']]['qty']   += r['qty']
    supplier_skus = []
    for sup_name in top_sup_names:
        items = sorted(
            [{'name': mat, **vals} for mat, vals in sup_sku_map[sup_name].items()],
            key=lambda x: x['value'], reverse=True
        )[:5]
        supplier_skus.append({'supplier': sup_name, 'items': items})

    # ── 6. Lead times ─────────────────────────────────────────────
    # A "commande" = unique (supplier, date) pair.
    # Multiple product lines on the same day for the same supplier
    # count as ONE order — deduplicate before computing gaps.
    date_qs = MaterialMovement.objects.filter(
        movement_type='ف شراء'
    ).values('customer_name', 'movement_date').distinct()

    if year_param != 'all':
        try:
            date_qs = date_qs.filter(movement_date__year=int(year_param))
        except (ValueError, TypeError):
            pass

    # Use a set per supplier so duplicate (supplier, date) rows are ignored
    sup_order_dates: dict[str, set] = defaultdict(set)
    for r in date_qs:
        sup_name = (r.get('customer_name') or '').strip() or 'Unknown'
        d = r['movement_date']
        if not d:
            continue
        # Normalise to a plain date so same-day datetime variants collapse
        if hasattr(d, 'date'):
            order_date = d.date()       # datetime → date
        elif hasattr(d, 'year'):
            order_date = d              # already a date
        else:
            try:
                order_date = datetime.fromisoformat(str(d)).date()
            except Exception:
                continue
        sup_order_dates[sup_name].add(order_date)

    lead_times = []
    for sup_name, date_set in sup_order_dates.items():
        # Need at least 2 distinct order dates to compute a gap
        if len(date_set) < 2:
            continue
        dates_sorted = sorted(date_set)
        gaps = [
            (dates_sorted[i + 1] - dates_sorted[i]).days
            for i in range(len(dates_sorted) - 1)
        ]
        # After dedup, gaps should all be > 0, but guard just in case
        positive_gaps = [g for g in gaps if g > 0]
        if not positive_gaps:
            continue
        avg_days = round(sum(positive_gaps) / len(positive_gaps))
        lead_times.append({
            'supplier': sup_name,
            'orders':   len(date_set),   # number of distinct order dates
            'avg_days': avg_days,
        })

    lead_times.sort(key=lambda x: x['avg_days'])
    lead_times = lead_times[:15]

    # ── 7. Response ───────────────────────────────────────────────
    return Response({
        'meta': {
            'total_value':        round(total_value, 2),
            'total_qty':          round(total_qty, 2),
            'unique_suppliers':   unique_suppliers,
            'unique_skus':        unique_skus,
            'total_transactions': len(filtered),
            'years':              sorted(all_years),
            'branches':           sorted(all_branches),
            'categories':         sorted(all_categories),
        },
        'monthly':       monthly,
        'by_branch':     by_branch,
        'by_supplier':   by_supplier,
        'by_category':   by_category,
        'branch_month':  branch_month,
        'supplier_skus': supplier_skus,
        'lead_times':    lead_times,
    })