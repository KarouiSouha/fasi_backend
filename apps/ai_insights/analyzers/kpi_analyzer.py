"""
apps/ai_insights/analyzers/kpi_analyzer.py
-------------------------------------------
SCRUM-24 v3.0 — KPI Analyzer

ARCHITECTURE FIX:
  Previous versions recomputed KPIs from scratch (MaterialMovement queries).
  v3.0 reads DIRECTLY from the existing apps/kpi/ computation results:

    apps/kpi/views.py        → credit KPIs (DSO, overdue ratio, collection efficiency)
    apps/kpi/views_sales.py  → sales KPIs (revenue, top products, margins, evolution)
    apps/kpi/views_stock.py  → stock KPIs (rotation, ruptures, coverage)

  This eliminates duplicate DB queries, keeps KPIs consistent between the
  KPI dashboard and the AI analysis, and benefits from all bug fixes already
  applied in the kpi app (correct movement_type filters, overdue calculation, etc.)

Branch filter: passed to sales/stock KPIs where supported.
"""

import logging
from datetime import date, timedelta

from apps.ai_insights.client import AIClient, AIClientError

logger = logging.getLogger(__name__)

DSO_TARGET_DAYS     = 60
CONCENTRATION_WARN  = 50.0
CONCENTRATION_CRIT  = 70.0
OVERDUE_WARN        = 0.20
OVERDUE_CRIT        = 0.50

SYSTEM_PROMPT = """You are a CFO-level financial analyst for WEEG, a BI platform \
serving Libyan distribution companies.

You receive business KPIs across 3 domains (credit, sales, stock), each with:
  - current value, prior value, delta %, traffic light status (green/amber/red)

Your job: write a concise, data-driven executive report.

Rules:
  1. Lead with the single most important insight — the number that changes a decision TODAY.
  2. Every claim must cite exact figures (LYD, %, days).
  3. Distinguish operational issues (fixable this week) from structural issues.
  4. Max 3 recommended actions — each must be executable by a named role.
  5. Currency: LYD (Libyan Dinar).

Return ONLY valid JSON — no markdown, no preamble:
{
  "executive_summary": "<3-4 sentences: what the KPIs reveal>",
  "top_insight": "<the single most important finding with exact numbers>",
  "health_score": <int 0-100>,
  "health_label": "excellent" | "good" | "fair" | "poor" | "critical",
  "kpi_commentary": {"<kpi_key>": "<one sentence specific to this KPI>"},
  "recommended_actions": [
    {"priority": <1-3>, "action": "<specific action>", "owner": "<role>", "impact": "<outcome with numbers>"}
  ],
  "risk_flags": ["<specific risk with number>"],
  "confidence": "high" | "medium" | "low"
}"""


class KPIAnalyzer:

    def __init__(self):
        self._client = AIClient()

    # ── Public ────────────────────────────────────────────────────────────────

    def analyze(self, company, use_ai: bool = True, branch: str = None) -> dict:
        logger.info("[KPIAnalyzer] company=%s branch=%s", company.id, branch)

        # 1. Read from existing KPI modules — no recomputation
        credit_kpis = self._fetch_credit_kpis(company)
        sales_kpis  = self._fetch_sales_kpis(company, branch=branch)
        stock_kpis  = self._fetch_stock_kpis(company, branch=branch)

        # 2. Merge into unified classified KPI dict
        classified = {}
        classified.update(self._build_credit_classified(credit_kpis))
        classified.update(self._build_sales_classified(sales_kpis))
        classified.update(self._build_stock_classified(stock_kpis))

        # 3. AI enrichment
        ai_result = None
        if use_ai:
            try:
                ai_result = self._call_ai(classified, company.id)
            except AIClientError as exc:
                logger.warning("[KPIAnalyzer] AI unavailable: %s", exc)

        return self._format_result(classified, ai_result, branch=branch,
                                    credit_raw=credit_kpis,
                                    sales_raw=sales_kpis,
                                    stock_raw=stock_kpis)

    # ── Fetch from existing KPI modules ───────────────────────────────────────

    @staticmethod
    def _fetch_credit_kpis(company) -> dict:
        """
        Calls the same logic as CreditKPIView (apps/kpi/views.py).
        Returns the raw kpis dict + summary.
        """
        try:
            from apps.aging.models import AgingReceivable, AgingSnapshot
            from apps.transactions.models import MaterialMovement
            from django.db.models import Sum, Count, Q
            from django.db.models.functions import Coalesce
            from decimal import Decimal

            latest_snap = (AgingSnapshot.objects.filter(company=company)
                           .order_by("-uploaded_at").first())
            aging_qs = (AgingReceivable.objects.filter(snapshot=latest_snap)
                        if latest_snap else AgingReceivable.objects.none())

            # Same excludes as CreditKPIView
            credit_aging_qs = aging_qs.exclude(
                Q(account__icontains="نقدي") | Q(account_code="1141001")
            )

            total_customers  = aging_qs.count()
            credit_customers = credit_aging_qs.filter(total__gt=0).values("account_code").distinct().count()

            CASH_FILTER = Q(customer_name__icontains="نقدي") | Q(customer_name__icontains="قطاعي")
            sales_qs  = MaterialMovement.objects.filter(company=company, movement_type="ف بيع")
            ca_total  = float(sales_qs.aggregate(ca=Coalesce(Sum("total_out"), Decimal("0")))["ca"])
            ca_credit = float(sales_qs.exclude(CASH_FILTER).exclude(
                Q(customer_name__isnull=True) | Q(customer_name="")
            ).aggregate(ca=Coalesce(Sum("total_out"), Decimal("0")))["ca"])

            # Aggregate aging buckets (same as CreditKPIView)
            ag = aging_qs.aggregate(
                total=Coalesce(Sum("total"),   Decimal("0")),
                current=Coalesce(Sum("current"), Decimal("0")),
                d61_90=Coalesce(Sum("d61_90"),  Decimal("0")),
                d91_120=Coalesce(Sum("d91_120"), Decimal("0")),
                d121_150=Coalesce(Sum("d121_150"), Decimal("0")),
                d151_180=Coalesce(Sum("d151_180"), Decimal("0")),
                d181_210=Coalesce(Sum("d181_210"), Decimal("0")),
                d211_240=Coalesce(Sum("d211_240"), Decimal("0")),
                d241_270=Coalesce(Sum("d241_270"), Decimal("0")),
                d271_300=Coalesce(Sum("d271_300"), Decimal("0")),
                d301_330=Coalesce(Sum("d301_330"), Decimal("0")),
                over_330=Coalesce(Sum("over_330"), Decimal("0")),
                d1_30=Coalesce(Sum("d1_30"),   Decimal("0")),
                d31_60=Coalesce(Sum("d31_60"),  Decimal("0")),
            )
            grand_total = float(ag["total"])
            current     = float(ag["current"])
            overdue     = sum(float(ag[b]) for b in [
                "d61_90","d91_120","d121_150","d151_180","d181_210",
                "d211_240","d241_270","d271_300","d301_330","over_330"
            ])

            # DSO = weighted avg (same BUCKET_MIDPOINTS as kpi/views.py)
            MIDS = {"current":0,"d1_30":15,"d31_60":45,"d61_90":75,"d91_120":105,
                    "d121_150":135,"d151_180":165,"d181_210":195,"d211_240":225,
                    "d241_270":255,"d271_300":285,"d301_330":315,"over_330":360}
            weighted = sum(MIDS[b] * float(ag[b]) for b in MIDS)
            dso = round(weighted / grand_total, 1) if grand_total > 0 else 0.0

            overdue_ratio     = overdue / grand_total if grand_total > 0 else 0.0
            collection_eff    = max(0.0, 1.0 - overdue_ratio) * 100
            taux_clients_cred = (credit_customers / total_customers * 100) if total_customers > 0 else 0.0

            # Taux recouvrement: what % of (CA - current receivables) has been collected
            # Use ca_total (all sales) so we always have a meaningful denominator
            montant_recupere  = max(0.0, ca_total - grand_total)
            taux_recouv       = round(montant_recupere / ca_total * 100, 2) if ca_total > 0 else 0.0

            # Top-5 risky (for context)
            top5 = list(
                credit_aging_qs.filter(total__gt=0).order_by("-total")[:5]
                .values("account", "account_code", "total", "current")
            )

            return {
                "grand_total_receivables": grand_total,
                "overdue_amount":          overdue,
                "current_amount":          current,
                "overdue_ratio":           overdue_ratio,
                "dso_days":                dso,
                "collection_efficiency":   collection_eff,
                "taux_clients_credit":     taux_clients_cred,
                "taux_recouvrement":       taux_recouv,
                "total_customers":         total_customers,
                "credit_customers":        credit_customers,
                "ca_total":                ca_total,
                "ca_credit":               ca_credit,
                "top5_risky":              top5,
                "snapshot_date":           str(latest_snap.uploaded_at.date()) if latest_snap else None,
            }
        except Exception as exc:
            logger.error("[KPIAnalyzer] credit fetch failed: %s", exc, exc_info=True)
            return {}

    @staticmethod
    def _fetch_sales_kpis(company, branch: str = None) -> dict:
        """
        Reuses SalesKPIView logic (apps/kpi/views_sales.py).
        Returns revenue, evolution, top products, monthly trend, margins.
        """
        try:
            from apps.transactions.models import MaterialMovement
            from django.db.models import Sum, Count, Q, F, Value, DecimalField, ExpressionWrapper
            from django.db.models.functions import Coalesce, TruncMonth
            from decimal import Decimal

            today = date.today()

            base_qs = MaterialMovement.objects.filter(company=company, movement_type="ف بيع")
            if branch:
                base_qs = base_qs.filter(branch__name=branch)

            # Detect the latest year that actually has data (mirrors SalesKPIView logic)
            latest_date = (
                base_qs.order_by("-movement_date")
                .values_list("movement_date", flat=True)
                .first()
            )
            year = latest_date.year if latest_date else today.year

            period_from = date(year, 1, 1)
            period_to   = date(year, 12, 31)
            prev_from   = date(year - 1, 1, 1)
            prev_to     = date(year - 1, 12, 31)

            sales_period = base_qs.filter(movement_date__gte=period_from, movement_date__lte=period_to)
            sales_prev   = base_qs.filter(movement_date__gte=prev_from,   movement_date__lte=prev_to)

            zero_dec = Value(Decimal("0.0000"), output_field=DecimalField(max_digits=18, decimal_places=4))
            profit_expr = ExpressionWrapper(
                (Coalesce(F("price_out"), zero_dec) - Coalesce(F("balance_price"), zero_dec))
                * Coalesce(F("qty_out"), zero_dec),
                output_field=DecimalField(max_digits=18, decimal_places=4),
            )

            ca_total = float(sales_period.aggregate(ca=Coalesce(Sum("total_out"), Decimal("0")))["ca"])
            ca_prev  = float(sales_prev.aggregate(ca=Coalesce(Sum("total_out"), Decimal("0")))["ca"])
            evolution = round((ca_total - ca_prev) / ca_prev * 100, 2) if ca_prev > 0 else None

            # Top 10 products
            top_products = list(
                sales_period
                .values("material_code", "material_name")
                .annotate(revenue=Coalesce(Sum("total_out"), Decimal("0")),
                          qty=Coalesce(Sum("qty_out"), Decimal("0")),
                          txns=Count("id"))
                .order_by("-revenue")[:10]
            )

            # Monthly trend
            monthly = list(
                sales_period
                .annotate(month=TruncMonth("movement_date"))
                .values("month")
                .annotate(revenue=Coalesce(Sum("total_out"), Decimal("0")))
                .order_by("month")
            )

            # Top 5 clients
            top_clients = list(
                sales_period
                .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
                .values("customer_name")
                .annotate(revenue=Coalesce(Sum("total_out"), Decimal("0")),
                          profit=Coalesce(Sum(profit_expr), Decimal("0")),
                          txns=Count("id"))
                .order_by("-revenue")[:5]
            )

            # Gross margin total
            gross_profit = float(
                sales_period.aggregate(p=Coalesce(Sum(profit_expr), Decimal("0")))["p"]
            )
            margin_pct = round(gross_profit / ca_total * 100, 2) if ca_total > 0 else 0.0

            n_days = max(1, (date(year, 12, 31) - date(year, 1, 1)).days + 1)

            return {
                "year":           year,
                "ca_total":       ca_total,
                "ca_prev":        ca_prev,
                "evolution_pct":  evolution,
                "gross_profit":   gross_profit,
                "margin_pct":     margin_pct,
                "top_products":   top_products,
                "monthly_sales":  monthly,
                "top_clients":    top_clients,
                "avg_daily_rev":  round(ca_total / n_days, 2),
            }
        except Exception as exc:
            logger.error("[KPIAnalyzer] sales fetch failed: %s", exc, exc_info=True)
            return {}

    @staticmethod
    def _fetch_stock_kpis(company, branch: str = None) -> dict:
        """
        Reuses StockKPIView logic (apps/kpi/views_stock.py).
        Returns stock totals, rotation, ruptures, coverage at risk.
        """
        try:
            from apps.inventory.models import InventorySnapshotLine
            from apps.transactions.models import MaterialMovement
            from django.db.models import Sum, DecimalField
            from django.db.models.functions import Coalesce
            from decimal import Decimal

            today = date.today()

            # Detect the latest year that actually has data (mirrors StockKPIView logic)
            _latest = (
                MaterialMovement.objects
                .filter(company=company, movement_type="ف بيع")
                .order_by("-movement_date")
                .values_list("movement_date", flat=True)
                .first()
            )
            year = _latest.year if _latest else today.year

            period_from = date(year, 1, 1)
            period_to   = date(year, 12, 31)
            n_days = (period_to - period_from).days + 1

            # Sales qty by product name (same join key as StockKPIView)
            sales_qs = (
                MaterialMovement.objects
                .filter(company=company, movement_type="ف بيع",
                        movement_date__gte=period_from, movement_date__lte=period_to)
                .values("material_name")
                .annotate(qty_sold=Coalesce(Sum("qty_out"), Decimal("0")))
            )
            sales_by_name = {
                (r["material_name"] or "").strip().lower(): float(r["qty_sold"])
                for r in sales_qs
            }

            inv_lines = InventorySnapshotLine.objects.filter(company=company)
            if branch:
                inv_lines = inv_lines.filter(branch_name=branch)

            agg = inv_lines.aggregate(
                total_qty=Coalesce(Sum("quantity"), Decimal("0")),
                total_value=Coalesce(Sum("line_value"), Decimal("0")),
            )
            total_qty   = float(agg["total_qty"])
            total_value = float(agg["total_value"])

            # Per-product rotation
            products = (
                inv_lines
                .values("product_name", "product_code")
                .annotate(stock_qty=Coalesce(Sum("quantity"), Decimal("0")),
                          stock_val=Coalesce(Sum("line_value"), Decimal("0")))
            )

            zero_stock_count = 0
            low_rotation     = []
            critical_count   = 0
            total_products   = 0

            for p in products:
                total_products += 1
                stock_qty   = float(p["stock_qty"])
                name_key    = (p["product_name"] or "").strip().lower()
                qty_sold    = sales_by_name.get(name_key, 0.0)
                rotation    = round(qty_sold / stock_qty, 4) if stock_qty > 0 else 0.0
                daily_sales = qty_sold / n_days if n_days > 0 else 0

                if stock_qty == 0:
                    zero_stock_count += 1
                else:
                    coverage = stock_qty / daily_sales if daily_sales > 0 else None
                    if coverage is not None and coverage < 14:
                        critical_count += 1
                    if rotation < 0.5 and stock_qty > 0:
                        low_rotation.append({
                            "product_code": p["product_code"],
                            "product_name": p["product_name"],
                            "stock_qty": stock_qty,
                            "stock_val": float(p["stock_val"]),
                            "rotation": rotation,
                        })

            avg_rotation = (
                sum(v for v in [
                    sales_by_name.get((p["product_name"] or "").strip().lower(), 0) /
                    float(p["stock_qty"]) if float(p["stock_qty"]) > 0 else 0
                    for p in products
                ]) / total_products
            ) if total_products > 0 else 0.0

            return {
                "total_products":     total_products,
                "total_stock_qty":    total_qty,
                "total_stock_value":  total_value,
                "zero_stock_count":   zero_stock_count,
                "low_rotation_count": len(low_rotation),
                "critical_coverage":  critical_count,
                "avg_rotation":       round(avg_rotation, 4),
                "low_rotation_items": low_rotation[:5],
            }
        except Exception as exc:
            logger.error("[KPIAnalyzer] stock fetch failed: %s", exc, exc_info=True)
            return {}

    # ── Build classified KPI dicts ────────────────────────────────────────────

    @staticmethod
    def _build_credit_classified(data: dict) -> dict:
        if not data:
            return {}
        classified = {}
        grand   = data.get("grand_total_receivables", 0)
        overdue = data.get("overdue_amount", 0)
        dso     = data.get("dso_days", 0)
        eff     = data.get("collection_efficiency", 0)
        ratio   = data.get("overdue_ratio", 0)

        # DSO
        dso_status = "green" if dso <= DSO_TARGET_DAYS else "amber" if dso <= DSO_TARGET_DAYS * 1.25 else "red"
        classified["dso_days"] = {"current": dso, "baseline": DSO_TARGET_DAYS,
                                   "delta_pct": round((dso - DSO_TARGET_DAYS) / DSO_TARGET_DAYS * 100, 1),
                                   "status": dso_status,
                                   "label": "DSO (avg payment days)",
                                   "source": "credit_kpi"}
        # Overdue ratio
        or_status = "green" if ratio < OVERDUE_WARN else "amber" if ratio < OVERDUE_CRIT else "red"
        classified["overdue_ratio"] = {"current": round(ratio, 4), "baseline": OVERDUE_WARN,
                                        "delta_pct": round(ratio * 100, 1),
                                        "status": or_status,
                                        "label": "Overdue ratio",
                                        "source": "credit_kpi"}
        # Collection efficiency
        eff_status = "green" if eff >= 80 else "amber" if eff >= 50 else "red"
        classified["collection_efficiency_pct"] = {"current": round(eff, 1), "baseline": 80.0,
                                                    "delta_pct": round(eff - 80, 1),
                                                    "status": eff_status,
                                                    "label": "Collection efficiency %",
                                                    "source": "credit_kpi"}
        # Total receivables
        classified["total_receivable_lyd"] = {"current": round(grand, 2), "baseline": 0,
                                               "delta_pct": 0, "status": "amber" if grand > 500_000 else "green",
                                               "label": "Total receivables (LYD)",
                                               "source": "credit_kpi"}
        # Overdue amount
        classified["overdue_lyd"] = {"current": round(overdue, 2), "baseline": 0,
                                      "delta_pct": 0, "status": or_status,
                                      "label": "Overdue amount (LYD)",
                                      "source": "credit_kpi"}
        # Taux recouvrement
        taux_r = data.get("taux_recouvrement", 0)
        tr_status = "green" if taux_r >= 70 else "amber" if taux_r >= 40 else "red"
        classified["taux_recouvrement_pct"] = {"current": round(taux_r, 1), "baseline": 70.0,
                                                "delta_pct": round(taux_r - 70, 1),
                                                "status": tr_status,
                                                "label": "Collection rate %",
                                                "source": "credit_kpi"}
        return classified

    @staticmethod
    def _build_sales_classified(data: dict) -> dict:
        if not data:
            return {}
        classified = {}
        ca    = data.get("ca_total", 0)
        evo   = data.get("evolution_pct")
        mg    = data.get("margin_pct", 0)
        daily = data.get("avg_daily_rev", 0)

        # Revenue with YoY evolution
        rev_status = "green" if evo and evo >= 5 else "amber" if evo and evo >= -5 else "red"
        if evo is None:
            rev_status = "amber"
        classified["total_revenue_lyd"] = {
            "current": round(ca, 2), "baseline": data.get("ca_prev", 0),
            "delta_pct": round(evo, 2) if evo is not None else 0,
            "status": rev_status,
            "label": f"Revenue YTD {data.get('year', '')} (LYD)",
            "source": "sales_kpi",
        }
        # Margin
        mg_status = "green" if mg >= 20 else "amber" if mg >= 10 else "red"
        classified["gross_margin_pct"] = {
            "current": round(mg, 2), "baseline": 20.0,
            "delta_pct": round(mg - 20, 2),
            "status": mg_status,
            "label": "Gross margin %",
            "source": "sales_kpi",
        }
        # Daily revenue
        classified["avg_daily_revenue_lyd"] = {
            "current": round(daily, 2), "baseline": 0, "delta_pct": 0,
            "status": "green" if daily > 10_000 else "amber",
            "label": "Avg daily revenue (LYD)",
            "source": "sales_kpi",
        }
        return classified

    @staticmethod
    def _build_stock_classified(data: dict) -> dict:
        if not data:
            return {}
        classified = {}
        zero   = data.get("zero_stock_count", 0)
        low_r  = data.get("low_rotation_count", 0)
        crit   = data.get("critical_coverage", 0)
        total  = data.get("total_products", 1) or 1
        value  = data.get("total_stock_value", 0)

        # Rupture rate
        rupture_pct = round(zero / total * 100, 1)
        rp_status   = "green" if rupture_pct < 5 else "amber" if rupture_pct < 15 else "red"
        classified["stock_rupture_pct"] = {
            "current": rupture_pct, "baseline": 5.0,
            "delta_pct": round(rupture_pct - 5, 1),
            "status": rp_status,
            "label": f"Out-of-stock rate % ({zero} SKUs)",
            "source": "stock_kpi",
        }
        # Critical coverage (< 14 days)
        crit_pct  = round(crit / total * 100, 1)
        cr_status = "green" if crit_pct < 5 else "amber" if crit_pct < 20 else "red"
        classified["critical_coverage_pct"] = {
            "current": crit_pct, "baseline": 5.0,
            "delta_pct": round(crit_pct - 5, 1),
            "status": cr_status,
            "label": f"SKUs with < 14d coverage ({crit} SKUs)",
            "source": "stock_kpi",
        }
        # Stock value
        classified["total_stock_value_lyd"] = {
            "current": round(value, 2), "baseline": 0, "delta_pct": 0,
            "status": "green",
            "label": "Total inventory value (LYD)",
            "source": "stock_kpi",
        }
        return classified

    # ── AI call ───────────────────────────────────────────────────────────────

    def _call_ai(self, classified: dict, company_id) -> dict | None:
        lines = []
        for key, v in classified.items():
            label  = v.get("label", key)
            sign   = "+" if v.get("delta_pct", 0) >= 0 else ""
            source = v.get("source", "")
            lines.append(
                f"  [{source}] {label:<45} "
                f"current={v['current']:>14,.2f}  "
                f"Δ={sign}{v['delta_pct']:.1f}%  [{v['status'].upper()}]"
            )
        user_prompt = (
            f"Business KPI Report — {date.today().isoformat()}\n"
            f"Source modules: credit_kpi, sales_kpi, stock_kpi\n\n"
            + "\n".join(lines)
            + "\n\nProvide an executive analysis for a Libyan B2B distribution company."
        )
        return self._client.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            model="smart", max_tokens=900,
            analyzer="kpi_analyzer", company_id=str(company_id),
        )

    # ── Output formatting ─────────────────────────────────────────────────────

    def _format_result(self, classified: dict, ai_result: dict | None,
                        branch: str = None, credit_raw: dict = None,
                        sales_raw: dict = None, stock_raw: dict = None) -> dict:
        statuses    = [v["status"] for v in classified.values()]
        red_count   = statuses.count("red")
        amber_count = statuses.count("amber")
        raw_score   = 100 - (red_count * 20) - (amber_count * 8)
        health_score = max(0, min(100, raw_score))

        health_label = (
            "excellent" if health_score >= 80 else
            "good"      if health_score >= 65 else
            "fair"      if health_score >= 50 else
            "poor"      if health_score >= 35 else "critical"
        )

        executive_summary   = self._default_summary(classified, credit_raw, sales_raw)
        top_insight         = self._default_top_insight(classified)
        kpi_commentary      = {k: f"{v['current']:,.2f} — {v['status']} ({v.get('label', k)})" for k, v in classified.items()}
        recommended_actions = self._default_actions(classified, credit_raw)
        risk_flags          = [f"[RED] {v.get('label', k)}: {v['current']:,.2f}"
                               for k, v in classified.items() if v["status"] == "red"] or ["No critical flags."]
        confidence          = "medium"

        if ai_result and not ai_result.get("error"):
            executive_summary   = ai_result.get("executive_summary",   executive_summary)
            top_insight         = ai_result.get("top_insight",         top_insight)
            kpi_commentary      = ai_result.get("kpi_commentary",      kpi_commentary)
            recommended_actions = ai_result.get("recommended_actions", recommended_actions)
            risk_flags          = ai_result.get("risk_flags",          risk_flags)
            confidence          = ai_result.get("confidence",          "medium")
            ai_score = ai_result.get("health_score")
            if isinstance(ai_score, int) and abs(ai_score - health_score) <= 15:
                health_score = ai_score
            health_label = ai_result.get("health_label", health_label)

        # Enrich with raw module data for frontend
        extra_context = {}
        if credit_raw:
            extra_context["credit"] = {
                "grand_total_receivables": credit_raw.get("grand_total_receivables", 0),
                "overdue_amount":          credit_raw.get("overdue_amount", 0),
                "dso_days":                credit_raw.get("dso_days", 0),
                "snapshot_date":           credit_raw.get("snapshot_date"),
                "top5_risky":              credit_raw.get("top5_risky", []),
            }
        if sales_raw:
            extra_context["sales"] = {
                "ca_total":      sales_raw.get("ca_total", 0),
                "ca_prev":       sales_raw.get("ca_prev", 0),
                "evolution_pct": sales_raw.get("evolution_pct"),
                "margin_pct":    sales_raw.get("margin_pct", 0),
                "year":          sales_raw.get("year"),
                "top_clients":   [
                    {"name": c["customer_name"], "revenue": float(c["revenue"])}
                    for c in (sales_raw.get("top_clients") or [])[:5]
                ],
                "top_products":  [
                    {"code": p["material_code"], "name": p["material_name"], "revenue": float(p["revenue"])}
                    for p in (sales_raw.get("top_products") or [])[:5]
                ],
            }
        if stock_raw:
            extra_context["stock"] = {
                "total_products":    stock_raw.get("total_products", 0),
                "zero_stock_count":  stock_raw.get("zero_stock_count", 0),
                "total_stock_value": stock_raw.get("total_stock_value", 0),
                "avg_rotation":      stock_raw.get("avg_rotation", 0),
            }

        return {
            "period_days":   30,
            "computed_at":   date.today().isoformat(),
            "branch_filter": branch,
            "data_sources":  ["credit_kpi", "sales_kpi", "stock_kpi"],
            "health_score":  health_score,
            "health_label":  health_label,
            "kpis":          classified,
            "executive_summary":    executive_summary,
            "top_insight":          top_insight,
            "kpi_commentary":       kpi_commentary,
            "recommended_actions":  recommended_actions,
            "risk_flags":           risk_flags,
            "extra_context":        extra_context,
            "summary": {
                "total_kpis": len(statuses),
                "green":      statuses.count("green"),
                "amber":      amber_count,
                "red":        red_count,
            },
            "confidence": confidence,
        }

    @staticmethod
    def _default_summary(classified, credit_raw, sales_raw) -> str:
        parts = []
        if sales_raw and sales_raw.get("ca_total"):
            evo = sales_raw.get("evolution_pct")
            sign = "+" if evo and evo >= 0 else ""
            parts.append(
                f"Revenue YTD {sales_raw.get('year')}: {sales_raw['ca_total']:,.0f} LYD"
                + (f" ({sign}{evo:.1f}% vs last year)" if evo is not None else "")
            )
        if credit_raw and credit_raw.get("dso_days"):
            dso = credit_raw["dso_days"]
            parts.append(f"DSO: {dso:.0f}d ({'above' if dso > DSO_TARGET_DAYS else 'within'} {DSO_TARGET_DAYS}d target)")
        if credit_raw and credit_raw.get("overdue_ratio"):
            r = credit_raw["overdue_ratio"]
            parts.append(f"Overdue: {r*100:.0f}% of receivables ({'critical' if r > 0.5 else 'concerning' if r > 0.2 else 'acceptable'})")
        return ". ".join(parts) + "." if parts else "KPI analysis complete."

    @staticmethod
    def _default_top_insight(classified) -> str:
        reds = [(k, v) for k, v in classified.items() if v["status"] == "red"]
        if not reds:
            reds = [(k, v) for k, v in classified.items() if v["status"] == "amber"]
        if not reds:
            return "All KPIs within acceptable ranges."
        worst_key, worst = max(reds, key=lambda x: abs(x[1].get("delta_pct", 0)))
        return f"{worst.get('label', worst_key)}: {worst['current']:,.2f} — {worst['status'].upper()}"

    @staticmethod
    def _default_actions(classified, credit_raw) -> list:
        actions = []
        dso  = classified.get("dso_days", {}).get("current", 0)
        over = classified.get("overdue_ratio", {}).get("current", 0)
        rupt = classified.get("stock_rupture_pct", {}).get("current", 0)
        if dso > DSO_TARGET_DAYS:
            actions.append({"priority": 1,
                "action": f"Accelerate collections — DSO {dso:.0f}d vs {DSO_TARGET_DAYS}d target",
                "owner": "Finance Manager", "impact": "Improve working capital"})
        if over > 0.5:
            actions.append({"priority": 2,
                "action": f"Recovery campaign for {over*100:.0f}% overdue receivables",
                "owner": "Credit Controller", "impact": "Reduce overdue exposure"})
        if rupt > 10:
            actions.append({"priority": 3,
                "action": f"Emergency reorder for {rupt:.0f}% out-of-stock SKUs",
                "owner": "Stock Manager", "impact": "Prevent lost sales"})
        if not actions:
            actions.append({"priority": 1, "action": "Maintain current performance", "owner": "Management", "impact": "Sustained advantage"})
        return actions[:3]