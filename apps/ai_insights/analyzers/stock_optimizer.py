"""
apps/ai_insights/analyzers/stock_optimizer.py
----------------------------------------------
SCRUM-28 v2.0

Improvements:
  1. Real stock from InventorySnapshotLine (not purchases - sales estimation)
  2. Dynamic safety stock per season (safety_stock × max(1.0, SI))
  3. Multi-class service levels: A=P95, B=P75, C=P50 lead time
"""

import logging
import math
from datetime import date, timedelta
from collections import defaultdict

from django.db.models import Count, Sum, Avg, Min, Max, Q, F
from django.db.models.functions import TruncDate

from apps.ai_insights.client import AIClient, AIClientError

logger = logging.getLogger(__name__)

ANALYSIS_WINDOW_DAYS = 90
LEAD_TIME_DAYS       = 14      # default — overridden per class below
LEAD_TIME_CLASS = {"A": 14, "B": 12, "C": 10}   # days (P95/P75/P50 approximation)
SERVICE_LEVEL_Z_CLASS = {"A": 1.645, "B": 1.150, "C": 0.842}   # 95% / 75% / 60%
ORDER_COST_LYD       = 500
HOLDING_COST_RATE    = 0.20
MAX_ITEMS            = 100
AI_MAX_ITEMS         = 5
AI_INTER_CALL_DELAY  = 2
ABC_A_THRESHOLD      = 0.80
ABC_B_THRESHOLD      = 0.95

SYSTEM_PROMPT = """You are a senior inventory manager for WEEG, a Libyan distribution BI platform.

For ONE Class A SKU with urgent reorder need, give a specific recommendation.
Return ONLY valid JSON:
{
  "recommendation_summary": "<2-3 sentences with exact numbers>",
  "urgency_reason": "<why action needed now>",
  "order_suggestion": {"quantity": <int>, "timing": "<when>", "rationale": "<why>"},
  "revenue_at_risk_lyd": <float>,
  "confidence": "high" | "medium" | "low"
}"""


class StockOptimizer:

    def __init__(self):
        self._client = AIClient()

    def optimize(self, company, use_ai: bool = True) -> dict:
        logger.info("[StockOptimizer] Starting for company=%s", company.id)
        items = self._compute_item_metrics(company)
        if not items:
            return {"error": "No stock data available for analysis.", "items": [], "summary": {}}

        items = self._abc_classify(items)

        # Load seasonal indices for dynamic safety stock (SCRUM-28 improvement)
        seasonal_indices = self._load_seasonal_indices(company)

        items = self._compute_reorder_params(items, seasonal_indices)
        items = self._compute_urgency(items)

        urgency_order = {"immediate": 0, "soon": 1, "watch": 2, "ok": 3}
        items.sort(key=lambda x: (x["abc_class"], urgency_order.get(x["urgency"], 4)))

        if use_ai:
            import time as _time
            candidates = [i for i in items if i["abc_class"] == "A"
                          and i["urgency"] in ("immediate", "soon")][:AI_MAX_ITEMS]
            for rank, item in enumerate(candidates, start=1):
                if rank > 1:
                    _time.sleep(AI_INTER_CALL_DELAY)
                try:
                    ai_result = self._call_ai(item, company.id, rank)
                    if ai_result and not ai_result.get("error"):
                        idx = next(j for j, i in enumerate(items)
                                   if i["product_code"] == item["product_code"])
                        items[idx]["ai_recommendation"]   = ai_result.get("recommendation_summary", "")
                        items[idx]["order_suggestion"]     = ai_result.get("order_suggestion", {})
                        items[idx]["revenue_at_risk_lyd"]  = float(ai_result.get("revenue_at_risk_lyd", 0))
                        items[idx]["confidence"]           = ai_result.get("confidence", "medium")
                except AIClientError as exc:
                    logger.warning("[StockOptimizer] AI failed rank %d: %s", rank, exc)

        summary = self._build_summary(items)
        logger.info("[StockOptimizer] Done: %d items for company=%s", len(items), company.id)
        return {
            "analysis_window_days": ANALYSIS_WINDOW_DAYS,
            "lead_time_days":       LEAD_TIME_DAYS,
            "service_level":        "A=95%, B=75%, C=60%",
            "total_sku_count":      len(items),
            "summary":              summary,
            "items":                items[:MAX_ITEMS],
        }

    # ── Real stock from InventorySnapshotLine ─────────────────────────────────
    # ── Real stock from InventorySnapshotLine ─────────────────────────────────
    def _get_real_stock(self, company, branch_name: str | None = None) -> dict:
        """
        تحميل المخزون الحقيقي من InventorySnapshotLine
        هذه هي النسخة المحسنة والمستقرة
        """
        try:
            from apps.inventory.models import InventorySnapshot, InventorySnapshotLine
            from django.db.models import Sum

            # الحصول على آخر snapshot مرفوع للشركة
            latest_snapshot = (
                InventorySnapshot.objects
                .filter(company=company)
                .order_by("-uploaded_at")
                .first()
            )

            if not latest_snapshot:
                logger.warning(
                    "[StockOptimizer] No InventorySnapshot found for company=%s. "
                    "Please upload an inventory Excel file.", company.id
                )
                return {}

            # بناء الاستعلام
            lines_qs = InventorySnapshotLine.objects.filter(snapshot=latest_snapshot)
            
            if branch_name:
                lines_qs = lines_qs.filter(branch_name__iexact=branch_name)

            # تجميع الكميات حسب كود المنتج
            agg_rows = (
                lines_qs
                .values("product_code")
                .annotate(total_qty=Sum("quantity"))
            )

            stock_map = {
                row["product_code"]: float(row["total_qty"] or 0)
                for row in agg_rows
                if row.get("product_code")
            }

            logger.info(
                "[StockOptimizer] Real stock loaded from snapshot %s (%s) → %d SKUs",
                latest_snapshot.id,
                latest_snapshot.uploaded_at.date(),
                len(stock_map)
            )

            return stock_map

        except Exception as exc:
            logger.error(
                "[StockOptimizer] Error loading real stock from InventorySnapshotLine: %s", 
                exc, exc_info=True
            )
            return {}

    def _compute_item_metrics(self, company) -> list:
        """ 
        يستخدم المخزون الحقيقي فقط من InventorySnapshotLine
        """
        from apps.transactions.models import MaterialMovement
        from django.db.models import Sum, Count, Min, Max, Q

        today = date.today()
        start_date = today - timedelta(days=ANALYSIS_WINDOW_DAYS)

        # === 1. تحميل المخزون الحقيقي ===
        real_stock = self._get_real_stock(company)
        
        if not real_stock:
            logger.error(
                "[StockOptimizer] Cannot proceed: No inventory snapshot found for company=%s", 
                company.id
            )
            return []

        # === 2. جلب بيانات المبيعات ===
        sales = (
            MaterialMovement.objects
            .filter(
                company=company,
                movement_type="ف بيع",
                movement_date__gte=start_date
            )
            .values("material_code", "material_name")
            .annotate(
                total_revenue=Sum("total_out"),
                total_qty_sold=Sum("qty_out"),
                transaction_count=Count("id"),
                first_sale=Min("movement_date"),
                last_sale=Max("movement_date"),
            )
            .exclude(Q(material_code__isnull=True) | Q(material_code=""))
            .order_by("-total_revenue")[:MAX_ITEMS]
        )

        if not sales:
            logger.warning("[StockOptimizer] No sales data in last %d days for company=%s", 
                          ANALYSIS_WINDOW_DAYS, company.id)
            return []

        # === 3. تحميل أسماء الفروع ===
        product_codes = [row["material_code"] for row in sales if row.get("material_code")]
        branch_map = self._get_branch_map(company, product_codes)   # سنضيفها بعد قليل

        # === 4. بناء الـ items ===
        items = []
        for row in sales:
            code = row["material_code"]
            name = row["material_name"] or code or "Unknown"

            total_rev = float(row.get("total_revenue") or 0)
            qty_sold = float(row.get("total_qty_sold") or 0)
            txn_count = row.get("transaction_count") or 1

            first_sale = row.get("first_sale")
            last_sale = row.get("last_sale")

            active_days = max(1, (last_sale - first_sale).days) if first_sale and last_sale else ANALYSIS_WINDOW_DAYS
            avg_daily_demand = qty_sold / active_days if active_days > 0 else 0
            revenue_per_unit = total_rev / qty_sold if qty_sold > 0 else 0

            current_stock = float(real_stock.get(code, 0))

            items.append({
                "product_code": code,
                "product_name": name,
                "branch_name": branch_map.get(code, "Unknown"),
                "total_revenue_lyd": round(total_rev, 2),
                "qty_sold": round(qty_sold, 2),
                "transaction_count": txn_count,
                "avg_daily_demand": round(avg_daily_demand, 4),
                "revenue_per_unit_lyd": round(revenue_per_unit, 2),
                "current_stock": round(current_stock, 2),
                "active_days": active_days,
                "stock_source": "real",
                "abc_class": None,
                "revenue_pct": 0.0,
                "cumulative_pct": 0.0,
                "reorder_point": 0.0,
                "safety_stock": 0.0,
                "eoq": 0,
                "estimated_days_to_stockout": None,
                "urgency": "ok",
                "ai_recommendation": "",
                "order_suggestion": {},
                "revenue_at_risk_lyd": 0.0,
                "confidence": "medium",
            })

        logger.info("[StockOptimizer] Loaded %d items with REAL stock", len(items))
        return items
    def _get_branch_map(self, company, product_codes: list) -> dict:
        """تحميل اسم الفرع لكل منتج"""
        if not product_codes:
            return {}

        from apps.transactions.models import MaterialMovement

        branch_map = {}
        try:
            for movement in MaterialMovement.objects.filter(
                company=company,
                material_code__in=product_codes,
                movement_type="ف بيع"
            ).exclude(branch__isnull=True).order_by('material_code', '-movement_date').values('material_code', 'branch__name'):
                
                code = movement.get('material_code')
                if code and code not in branch_map:
                    branch_map[code] = movement.get('branch__name') or "Unknown"
        except Exception:
            pass

        # تعبئة الباقي
        for code in product_codes:
            if code not in branch_map:
                branch_map[code] = "Unknown"

        return branch_map
    
    # ── Seasonal indices for dynamic safety stock ─────────────────────────────

    @staticmethod
    def _load_seasonal_indices(company) -> dict:
        """Load SI from cache if available; fallback to 1.0 for all months."""
        from django.core.cache import cache
        key = f"ai:seasonal:{company.id}:ai1"
        data = cache.get(key)
        if data and isinstance(data.get("seasonality_indices"), dict):
            si_raw = data["seasonality_indices"]
            return {int(m): (v.get("seasonality_index") or 1.0)
                    for m, v in si_raw.items()}
        return {m: 1.0 for m in range(1, 13)}

    # ── ABC ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _abc_classify(items: list) -> list:
        items.sort(key=lambda x: -x["total_revenue_lyd"])
        total_rev  = sum(i["total_revenue_lyd"] for i in items) or 1
        cumulative = 0.0
        for item in items:
            pct        = item["total_revenue_lyd"] / total_rev
            cumulative += pct
            item["revenue_pct"]    = round(pct * 100, 3)
            item["cumulative_pct"] = round(cumulative * 100, 3)
            item["abc_class"] = ("A" if cumulative <= ABC_A_THRESHOLD else
                                 "B" if cumulative <= ABC_B_THRESHOLD else "C")
        return items

    # ── Reorder params with dynamic safety stock ──────────────────────────────

    @staticmethod
    def _compute_reorder_params(items: list, seasonal_indices: dict) -> list:
        """
        v2.0: Per-class service level + seasonal safety stock multiplier.
        safety_stock = Z_class × σ_demand × √lead_time × max(1.0, SI_current_month)
        """
        current_month = date.today().month
        current_si    = seasonal_indices.get(current_month, 1.0)
        # Peak modifier: amplify safety stock only during above-average months
        season_multiplier = max(1.0, current_si)

        for item in items:
            d     = item["avg_daily_demand"]
            abc   = item["abc_class"]
            z     = SERVICE_LEVEL_Z_CLASS.get(abc, 1.0)
            lt    = LEAD_TIME_CLASS.get(abc, LEAD_TIME_DAYS)

            if d <= 0:
                item["reorder_point"] = 0
                item["safety_stock"]  = 0
                item["eoq"]           = 0
                continue

            sigma_d      = 0.20 * d   # 20% CV assumption
            safety_stock = z * sigma_d * math.sqrt(lt) * season_multiplier
            rop          = (d * lt) + safety_stock

            # EOQ
            annual_demand = d * 365
            unit_cost     = item["revenue_per_unit_lyd"] or 1
            holding_cost  = HOLDING_COST_RATE * unit_cost
            eoq = math.sqrt((2 * annual_demand * ORDER_COST_LYD) / holding_cost) if holding_cost > 0 else 0

            item["reorder_point"]       = round(rop, 1)
            item["safety_stock"]        = round(safety_stock, 1)
            item["safety_stock_raw"]    = round(z * sigma_d * math.sqrt(lt), 1)  # without season boost
            item["season_multiplier"]   = round(season_multiplier, 3)
            item["eoq"]                 = round(eoq)
        return items

    @staticmethod
    def _compute_urgency(items: list) -> list:
        for item in items:
            d     = item["avg_daily_demand"]
            stock = item["current_stock"]
            rop   = item["reorder_point"]
            lt    = LEAD_TIME_CLASS.get(item["abc_class"], LEAD_TIME_DAYS)
            if d > 0:
                days_to_out = stock / d
                item["estimated_days_to_stockout"] = round(days_to_out, 1)
            else:
                days_to_out = 999
                item["estimated_days_to_stockout"] = None

            if stock <= 0 or days_to_out < lt:         item["urgency"] = "immediate"
            elif stock <= rop:                          item["urgency"] = "soon"
            elif stock <= rop * 1.5:                   item["urgency"] = "watch"
            else:                                      item["urgency"] = "ok"
        return items

    def _call_ai(self, item: dict, company_id, rank: int) -> dict | None:
        d        = item["avg_daily_demand"]
        stock    = item["current_stock"]
        days_out = item["estimated_days_to_stockout"]
        unit_rev = item["revenue_per_unit_lyd"]
        lt       = LEAD_TIME_CLASS.get(item["abc_class"], LEAD_TIME_DAYS)
        user_prompt = (
            f"SKU-{rank:03d} (Class {item['abc_class']}) | "
            f"Stock source: {item.get('stock_source', 'estimate')}\n"
            f"Demand: {d:.2f} units/day | Stock: {stock:.0f} units | "
            f"Days to stockout: {f'{days_out:.1f}' if days_out else 'NOW'}\n"
            f"ROP: {item['reorder_point']:.0f} | Safety stock: {item['safety_stock']:.0f} "
            f"(×{item.get('season_multiplier', 1):.2f} seasonal) | EOQ: {item['eoq']}\n"
            f"Unit revenue: {unit_rev:,.2f} LYD | Daily revenue: {d * unit_rev:,.2f} LYD\n"
            f"Lead time ({item['abc_class']}-class): {lt} days | Urgency: {item['urgency'].upper()}"
        )
        return self._client.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            model="smart", max_tokens=500,
            analyzer="stock_optimizer", company_id=str(company_id),
        )

    @staticmethod
    def _build_summary(items: list) -> dict:
        return {
            "total_items":            len(items),
            "class_a_count":          sum(1 for i in items if i["abc_class"] == "A"),
            "class_b_count":          sum(1 for i in items if i["abc_class"] == "B"),
            "class_c_count":          sum(1 for i in items if i["abc_class"] == "C"),
            "immediate_reorders":     sum(1 for i in items if i["urgency"] == "immediate"),
            "soon_reorders":          sum(1 for i in items if i["urgency"] == "soon"),
            "items_at_or_below_rop":  sum(1 for i in items if i["current_stock"] <= i["reorder_point"]),
            "total_revenue_covered_lyd": sum(i["total_revenue_lyd"] for i in items),
        }