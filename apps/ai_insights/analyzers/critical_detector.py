"""
apps/ai_insights/analyzers/critical_detector.py
-------------------------------------------------
SCRUM-35 v2.0 - Causal graph linking + role-based situation filtering
"""

import logging
from datetime import date, timedelta
from django.db.models import Count, Sum, Q, Max
from apps.ai_insights.client import AIClient, AIClientError

logger = logging.getLogger(__name__)

WEIGHT_CHURN    = 0.30
WEIGHT_ANOMALY  = 0.25
WEIGHT_AGING    = 0.20
WEIGHT_STOCK    = 0.15
WEIGHT_KPI      = 0.10
MAX_SITUATIONS  = 10
ANALYSIS_DAYS   = 90

# ── Role → allowed signal sources ────────────────────────────────────────────
ROLE_PERMISSIONS = {
    "admin":   {"churn", "anomaly", "aging", "stock", "kpi"},
    "manager": {"churn", "anomaly", "aging", "stock", "kpi"},
    "agent":   {"stock", "anomaly"},    # field agents see stock + anomalies only
    "finance": {"aging", "kpi"},
    "sales":   {"churn", "anomaly"},
}
DEFAULT_SOURCES = {"churn", "anomaly", "aging", "stock", "kpi"}

SYSTEM_PROMPT = """You are the Chief Risk Officer for WEEG, a BI platform for Libyan distribution companies.

You receive critical business situations, some possibly causally linked.
Return ONLY valid JSON:
{
  "executive_briefing": "<4-6 sentences>",
  "total_exposure_lyd": <float>,
  "risk_level": "critical" | "high" | "medium" | "low",
  "causal_clusters": [
    {"cluster_name": "<name>", "situations": ["<title1>", "<title2>"],
     "common_cause": "<inferred root cause>", "unified_action": "<single action for cluster>"}
  ],
  "grouped_actions": {
    "act_within_24h": [{"situation": "<t>", "action": "<a>", "owner": "<r>"}],
    "act_this_week":  [{"situation": "<t>", "action": "<a>", "owner": "<r>"}],
    "monitor":        [{"situation": "<t>", "action": "<a>", "owner": "<r>"}]
  },
  "confidence": "high" | "medium" | "low"
}"""


class CriticalDetector:

    def __init__(self):
        self._client = AIClient()

    def detect(self, company, use_ai: bool = True, user_role: str = "manager") -> dict:
        logger.info("[CriticalDetector] Starting for company=%s role=%s", company.id, user_role)

        # Role-based source filtering (SCRUM-35 improvement)
        allowed_sources = ROLE_PERMISSIONS.get(user_role, DEFAULT_SOURCES)

        situations = []
        if "churn"   in allowed_sources: situations.extend(self._scan_churn_signals(company))
        if "anomaly" in allowed_sources: situations.extend(self._scan_anomaly_signals(company))
        if "aging"   in allowed_sources: situations.extend(self._scan_aging_signals(company))
        if "stock"   in allowed_sources: situations.extend(self._scan_stock_signals(company))
        if "kpi"     in allowed_sources: situations.extend(self._scan_kpi_signals(company))

        situations = self._deduplicate(situations)
        situations.sort(key=lambda s: -(s["composite_score"] * max(1, s["financial_exposure_lyd"])))
        top_situations = situations[:MAX_SITUATIONS]

        # Causal graph linking (SCRUM-35 improvement)
        causal_clusters = self._detect_causal_clusters(top_situations)

        total_exposure = sum(s["financial_exposure_lyd"] for s in top_situations)

        if top_situations:
            top_score  = top_situations[0]["composite_score"]
            risk_level = ("critical" if top_score >= 0.75 else
                          "high"     if top_score >= 0.55 else
                          "medium"   if top_score >= 0.35 else "low")
        else:
            risk_level = "low"

        executive_briefing = ""
        grouped_actions    = {}
        confidence         = "medium"

        if use_ai and top_situations:
            try:
                ai_result = self._call_ai(top_situations, causal_clusters, total_exposure, company.id)
                if ai_result and not ai_result.get("error"):
                    executive_briefing = ai_result.get("executive_briefing", "")
                    grouped_actions    = ai_result.get("grouped_actions",    {})
                    causal_clusters    = ai_result.get("causal_clusters",    causal_clusters)
                    confidence         = ai_result.get("confidence",         "medium")
                    ai_risk = ai_result.get("risk_level")
                    if ai_risk in ("critical", "high", "medium", "low"):
                        risk_level = ai_risk
                    ai_exp = ai_result.get("total_exposure_lyd", 0)
                    if ai_exp > 0:
                        total_exposure = ai_exp
            except AIClientError as exc:
                logger.warning("[CriticalDetector] AI unavailable: %s", exc)

        if not executive_briefing:
            executive_briefing = self._default_briefing(top_situations, total_exposure, risk_level)
        if not grouped_actions:
            grouped_actions = self._default_grouped_actions(top_situations)

        logger.info("[CriticalDetector] Done: %d situations, risk=%s for company=%s",
                    len(top_situations), risk_level, company.id)
        return {
            "generated_at":       date.today().isoformat(),
            "user_role":          user_role,
            "allowed_sources":    list(allowed_sources),
            "critical_count":     sum(1 for s in top_situations if s["composite_score"] >= 0.60),
            "total_situations":   len(top_situations),
            "total_exposure_lyd": round(total_exposure, 2),
            "risk_level":         risk_level,
            "executive_briefing": executive_briefing,
            "situations":         top_situations,
            "causal_clusters":    causal_clusters,
            "grouped_actions":    grouped_actions,
            "confidence":         confidence,
        }

    # ── Causal graph linking ──────────────────────────────────────────────────

    @staticmethod
    def _detect_causal_clusters(situations: list) -> list:
        """
        Detect when multiple situations likely share a common root cause.
        Heuristic rules based on signal type co-occurrence.

        Cluster definitions:
          "supply_chain_crisis":  stock + anomaly (revenue drop) on same period
          "customer_credit_risk": churn + aging on same period
          "demand_shock":         anomaly (spike) + churn improvement
          "cash_flow_stress":     aging + kpi (DSO)
        """
        clusters = []

        sources_present = {s["source"] for s in situations}
        severity_map    = {s["source"]: s["severity"] for s in situations}
        exposure_by_src = {s["source"]: s["financial_exposure_lyd"] for s in situations}

        # 1. Supply chain crisis: stock stockout + revenue drop anomaly
        if "stock" in sources_present and "anomaly" in sources_present:
            stock_sits   = [s for s in situations if s["source"] == "stock"]
            anomaly_sits = [s for s in situations if s["source"] == "anomaly"
                            and s.get("direction") == "drop"]
            if stock_sits and anomaly_sits:
                clusters.append({
                    "cluster_name": "Supply chain disruption",
                    "situations":   [s["title"] for s in stock_sits + anomaly_sits],
                    "common_cause": "Stock-outs are likely causing the revenue drop — customers cannot buy what is not available.",
                    "unified_action": "Emergency purchase orders for critical SKUs + customer communication about delays.",
                    "combined_exposure_lyd": sum(s["financial_exposure_lyd"]
                                                 for s in stock_sits + anomaly_sits),
                })

        # 2. Customer credit risk: churn + aging
        if "churn" in sources_present and "aging" in sources_present:
            churn_sits = [s for s in situations if s["source"] == "churn"]
            aging_sits = [s for s in situations if s["source"] == "aging"]
            if churn_sits and aging_sits:
                clusters.append({
                    "cluster_name": "Customer credit deterioration",
                    "situations":   [s["title"] for s in churn_sits + aging_sits],
                    "common_cause": "Customers with overdue payments are becoming inactive — payment stress is driving churn.",
                    "unified_action": "Structured payment plan offer before credit suspension — combine finance + account manager.",
                    "combined_exposure_lyd": sum(s["financial_exposure_lyd"]
                                                 for s in churn_sits + aging_sits),
                })

        # 3. Cash flow stress: aging + KPI (DSO)
        if "aging" in sources_present and "kpi" in sources_present:
            aging_sits = [s for s in situations if s["source"] == "aging"]
            kpi_sits   = [s for s in situations if s["source"] == "kpi"]
            if aging_sits and kpi_sits:
                clusters.append({
                    "cluster_name": "Cash flow stress",
                    "situations":   [s["title"] for s in aging_sits + kpi_sits],
                    "common_cause": "Rising DSO and overdue receivables are compressing working capital simultaneously.",
                    "unified_action": "Accelerated collections campaign: prioritize top-10 overdue accounts this week.",
                    "combined_exposure_lyd": sum(s["financial_exposure_lyd"]
                                                 for s in aging_sits + kpi_sits),
                })

        return clusters

    # ── Signal scanners ───────────────────────────────────────────────────────

    def _scan_churn_signals(self, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            today       = date.today()
            period_from = today - timedelta(days=ANALYSIS_DAYS)
            top_customers = (
                MaterialMovement.objects
                .filter(company=company, movement_type="ف بيع", movement_date__gte=period_from)
                .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
                .values("customer_name")
                .annotate(last_purchase=Max("movement_date"), total_revenue=Sum("total_out"))
                .order_by("-total_revenue")[:30]
            )
            revenues = [float(c["total_revenue"] or 0) for c in top_customers]
            avg_rev  = sum(revenues) / len(revenues) if revenues else 0
            situations = []
            for customer in top_customers:
                last = customer["last_purchase"]
                if not last:
                    continue
                days_inactive = (today - last).days
                # Cap à 365j — au-delà c'est un client inactif archivé, pas un signal utile
                if days_inactive > 365:
                    continue
                revenue = float(customer["total_revenue"] or 0)
                if days_inactive < 45 or revenue < avg_rev * 0.5:
                    continue
                recency_score  = min(1.0, days_inactive / 90)
                revenue_weight = min(1.0, revenue / max(revenues)) if revenues else 0.5
                score          = WEIGHT_CHURN * (recency_score * 0.7 + revenue_weight * 0.3)
                situations.append({
                    "source": "churn",
                    "title": f"Key customer inactive for {days_inactive} days",
                    "customer_name": customer["customer_name"],   # display only
                    "severity": "critical" if days_inactive > 90 else "high",
                    "composite_score": round(score, 4),
                    "summary": (f"{customer['customer_name']} has not ordered in {days_inactive} days. "
                                f"At-risk revenue: {revenue:,.0f} LYD."),
                    "financial_exposure_lyd": round(revenue * (days_inactive / ANALYSIS_DAYS), 2),
                    "recommended_action": "Immediate executive outreach within 24 hours.",
                    "urgency_hours": 24 if days_inactive > 90 else 72,
                })
            return situations[:3]
        except Exception as exc:
            logger.warning("[CriticalDetector] Churn scan failed: %s", exc)
            return []

    def _scan_anomaly_signals(self, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            today      = date.today()
            week_start = today - timedelta(days=7)
            base_start = today - timedelta(days=60)
            base_end   = today - timedelta(days=7)
            recent = (MaterialMovement.objects
                      .filter(company=company, movement_type="ف بيع", movement_date__gte=week_start)
                      .aggregate(total=Sum("total_out"), txns=Count("id")))
            recent_rev = float(recent["total"] or 0)
            baseline   = (MaterialMovement.objects
                          .filter(company=company, movement_type="ف بيع",
                                  movement_date__gte=base_start, movement_date__lt=base_end)
                          .aggregate(total=Sum("total_out")))
            baseline_7d = float(baseline["total"] or 0) / 53 * 7 if baseline["total"] else 0
            if baseline_7d <= 0:
                return []
            delta_pct = (recent_rev - baseline_7d) / baseline_7d * 100
            if delta_pct >= -15:
                return []
            exposure = abs(recent_rev - baseline_7d)
            score    = WEIGHT_ANOMALY * min(1.0, abs(delta_pct) / 60)
            return [{
                "source": "anomaly", "direction": "drop",
                "title": f"Revenue {delta_pct:.0f}% below baseline this week",
                "severity": "critical" if delta_pct < -40 else "high",
                "composite_score": round(score, 4),
                "summary": (f"Weekly revenue {recent_rev:,.0f} LYD vs expected {baseline_7d:,.0f} LYD "
                             f"({delta_pct:.0f}%). Shortfall: {exposure:,.0f} LYD."),
                "financial_exposure_lyd": round(exposure * 4, 2),
                "recommended_action": "Investigate sales pipeline and stock availability immediately.",
                "urgency_hours": 24,
            }]
        except Exception as exc:
            logger.warning("[CriticalDetector] Anomaly scan failed: %s", exc)
            return []

    def _scan_aging_signals(self, company) -> list:
        try:
            from apps.aging.models import AgingReceivable, AgingSnapshot
            latest_snap = (AgingSnapshot.objects.filter(company=company)
                           .order_by("-uploaded_at").first())
            if not latest_snap:
                return []
            situations = []
            for rec in AgingReceivable.objects.filter(snapshot=latest_snap).exclude(Q(total__lte=0)):
                total   = float(rec.total or 0)
                current = float(rec.current or 0)
                overdue = max(0.0, total - current)
                if total <= 0:
                    continue
                overdue_ratio = overdue / total
                if overdue_ratio < 0.60 or total < 50_000:
                    continue
                score = WEIGHT_AGING * min(1.0, overdue_ratio * (total / 500_000))
                account_label = rec.account or rec.account_code or "Unknown account"
                situations.append({
                    "source": "aging",
                    "title": f"{int(overdue_ratio*100)}% overdue — {total:,.0f} LYD at risk",
                    "account_name": account_label,   # display only
                    "severity": "critical" if overdue_ratio > 0.75 else "high",
                    "composite_score": round(score, 4),
                    "summary": (f"{account_label}: {overdue:,.0f} LYD overdue "
                                f"({overdue_ratio*100:.0f}% of {total:,.0f} LYD outstanding). "
                                f"Risk class: {rec.risk_score or 'unknown'}."),
                    "financial_exposure_lyd": round(overdue, 2),
                    "recommended_action": "Freeze credit and initiate formal collections.",
                    "urgency_hours": 48,
                })
            situations.sort(key=lambda s: -s["financial_exposure_lyd"])
            return situations[:3]
        except Exception as exc:
            logger.warning("[CriticalDetector] Aging scan failed: %s", exc)
            return []

    def _scan_stock_signals(self, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            today       = date.today()
            period_from = today - timedelta(days=ANALYSIS_DAYS)
            top_products = (
                MaterialMovement.objects
                .filter(company=company, movement_type="ف بيع", movement_date__gte=period_from)
                .values("material_code", "material_name")
                .annotate(total_revenue=Sum("total_out"), total_qty_sold=Sum("qty_out"))
                .exclude(Q(material_code__isnull=True) | Q(material_code=""))
                .order_by("-total_revenue")[:10]
            )
            purchases = dict(
                MaterialMovement.objects
                .filter(company=company, movement_type__contains="شراء", movement_date__gte=period_from)
                .values("material_code").annotate(total_in=Sum("qty_in"))
                .values_list("material_code", "total_in")
            )
            situations = []
            for prod in top_products:
                code       = prod["material_code"]
                revenue    = float(prod["total_revenue"] or 0)
                qty_sold   = float(prod["total_qty_sold"] or 0)
                qty_in     = float(purchases.get(code, 0) or 0)
                stock_est  = max(0, qty_in - qty_sold)
                daily_demand = qty_sold / ANALYSIS_DAYS if ANALYSIS_DAYS > 0 else 0
                if daily_demand <= 0:
                    continue
                days_to_out = stock_est / daily_demand
                if days_to_out > 14:
                    continue
                unit_rev = revenue / qty_sold if qty_sold > 0 else 0
                exposure = daily_demand * 14 * unit_rev
                score    = WEIGHT_STOCK * min(1.0, 1 - days_to_out / 14)
                situations.append({
                    "source": "stock",
                    "title": f"Class A item stockout in {days_to_out:.0f} days",
                    "severity": "critical" if days_to_out < 3 else "high",
                    "composite_score": round(score, 4),
                    "summary": (f"{(prod.get('material_name') or code)[:30]}: "
                                f"{stock_est:.0f} units remaining ({days_to_out:.0f}d at current demand). "
                                f"2-week revenue at risk: {exposure:,.0f} LYD."),
                    "financial_exposure_lyd": round(exposure, 2),
                    "recommended_action": "Place emergency purchase order immediately.",
                    "urgency_hours": 24 if days_to_out < 3 else 48,
                })
            return situations[:3]
        except Exception as exc:
            logger.warning("[CriticalDetector] Stock scan failed: %s", exc)
            return []

    def _scan_kpi_signals(self, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            from apps.aging.models import AgingReceivable, AgingSnapshot
            today      = date.today()
            curr_start = today - timedelta(days=30)
            prev_start = today - timedelta(days=60)
            prev_end   = today - timedelta(days=30)
            curr = (MaterialMovement.objects
                    .filter(company=company, movement_type="ف بيع", movement_date__gte=curr_start)
                    .aggregate(revenue=Sum("total_out")))
            prev = (MaterialMovement.objects
                    .filter(company=company, movement_type="ف بيع",
                            movement_date__gte=prev_start, movement_date__lt=prev_end)
                    .aggregate(revenue=Sum("total_out")))
            curr_rev = float(curr["revenue"] or 0)
            prev_rev = float(prev["revenue"] or 0)
            situations = []
            if prev_rev > 0:
                delta_pct = (curr_rev - prev_rev) / prev_rev * 100
                if delta_pct < -20:
                    score = WEIGHT_KPI * min(1.0, abs(delta_pct) / 50)
                    situations.append({
                        "source": "kpi",
                        "title": f"Revenue declined {delta_pct:.0f}% MoM",
                        "severity": "critical" if delta_pct < -35 else "high",
                        "composite_score": round(score, 4),
                        "summary": (f"Revenue dropped {prev_rev:,.0f} → {curr_rev:,.0f} LYD "
                                    f"({delta_pct:.0f}%). Annualized risk: {abs(curr_rev-prev_rev)*12:,.0f} LYD."),
                        "financial_exposure_lyd": round(abs(curr_rev - prev_rev) * 3, 2),
                        "recommended_action": "Emergency sales review — identify lost accounts.",
                        "urgency_hours": 48,
                    })
            # snap = AgingSnapshot.objects.filter(company=company).order_by("-uploaded_at").first()
            # if snap:
            #     ag = AgingReceivable.objects.filter(snapshot=snap).aggregate(
            #         total=Sum("total"), current=Sum("current")
            #     )
            #     total_rec  = float(ag["total"]   or 0)
            #     total_curr = float(ag["current"] or 0)
            #     overdue    = max(0.0, total_rec - total_curr)
            #     daily_rev  = curr_rev / 30 if curr_rev > 0 else 1
            #     dso        = total_rec / daily_rev if daily_rev > 0 else 0
            #     if dso > 75:
            # APRÈS — DSO sur 90j de revenus + cap à 730j
            snap = AgingSnapshot.objects.filter(company=company).order_by("-uploaded_at").first()
            if snap:
                ag = AgingReceivable.objects.filter(snapshot=snap).aggregate(
                    total=Sum("total"), current=Sum("current")
                )
                total_rec  = float(ag["total"]   or 0)
                total_curr = float(ag["current"] or 0)
                overdue    = max(0.0, total_rec - total_curr)

                # Utiliser 90j de revenus pour un daily_rev stable
                # (évite les DSO aberrants quand curr_rev est partiel ou nul)
                rev_90d = (MaterialMovement.objects
                        .filter(company=company, movement_type="ف بيع",
                                movement_date__gte=today - timedelta(days=90))
                        .aggregate(total=Sum("total_out")))
                rev_90d_val = float(rev_90d["total"] or 0)
                daily_rev   = rev_90d_val / 90 if rev_90d_val > 0 else None

                # Pas de revenus sur 90j → DSO incalculable, on skip
                if not daily_rev:
                    return situations

                dso = total_rec / daily_rev

                # Cap à 730 jours (2 ans max) — au-delà c'est un artefact de données
                dso = min(dso, 730)

                if dso > 75:
                    score = WEIGHT_KPI * min(1.0, (dso - 60) / 60)
                    situations.append({
                        "source": "kpi",
                        "title": f"DSO critical at {dso:.0f} days",
                        "severity": "critical" if dso > 100 else "high",
                        "composite_score": round(score, 4),
                        "summary": (f"DSO {dso:.0f} days ({dso - 60:.0f}d above 60-day target). "
                                    f"Receivables: {total_rec:,.0f} LYD."),
                        "financial_exposure_lyd": round(total_rec * 0.15, 2),
                        "recommended_action": "Immediate collections campaign.",
                        "urgency_hours": 72,
                    })
            return situations
        except Exception as exc:
            logger.warning("[CriticalDetector] KPI scan failed: %s", exc)
            return []

    @staticmethod
    def _deduplicate(situations: list) -> list:
        seen: dict = {}
        result = []
        for s in situations:
            src   = s["source"]
            count = seen.get(src, 0)
            if count >= 2:
                continue
            seen[src] = count + 1
            result.append(s)
        return result

    def _call_ai(self, situations, causal_clusters, total_exposure, company_id) -> dict | None:
        sit_lines = [
            f"  {i}. [{s['source'].upper()}] {s['title']} | "
            f"Score={s['composite_score']:.3f} | "
            f"Exposure={s['financial_exposure_lyd']:,.0f} LYD | "
            f"Urgency={s['urgency_hours']}h"
            for i, s in enumerate(situations, 1)
        ]
        cluster_lines = [
            f"  CLUSTER: {c['cluster_name']} | Cause: {c['common_cause'][:80]}"
            for c in causal_clusters
        ]
        user_prompt = (
            f"Executive Risk Briefing — {date.today().isoformat()}\n"
            f"Total exposure: {total_exposure:,.0f} LYD | Situations: {len(situations)}\n\n"
            f"Situations:\n" + "\n".join(sit_lines) +
            ("\n\nCausal clusters detected:\n" + "\n".join(cluster_lines) if cluster_lines else "") +
            "\n\nProvide a CEO briefing with grouped actions and causal cluster analysis."
        )
        return self._client.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            model="smart", max_tokens=1000,
            analyzer="critical_detector", company_id=str(company_id),
        )

    @staticmethod
    def _default_briefing(situations, total_exposure, risk_level) -> str:
        if not situations:
            return "No critical situations detected. All business metrics within acceptable ranges."
        top  = situations[0]
        crit = sum(1 for s in situations if s["severity"] == "critical")
        return (f"Risk level: {risk_level.upper()}. {len(situations)} situations detected "
                f"({crit} critical). Total exposure: {total_exposure:,.0f} LYD. "
                f"Most urgent: {top['title']}. Act within {top['urgency_hours']}h.")

    @staticmethod
    def _default_grouped_actions(situations) -> dict:
        to_action = lambda s: {"situation": s["title"],
                                "action": s["recommended_action"],
                                "owner": "Operations Manager"}
        return {
            "act_within_24h": [to_action(s) for s in situations if s["urgency_hours"] <= 24],
            "act_this_week":  [to_action(s) for s in situations if 24 < s["urgency_hours"] <= 120],
            "monitor":        [to_action(s) for s in situations if s["urgency_hours"] > 120],
        }