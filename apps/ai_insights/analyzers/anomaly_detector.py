"""
apps/ai_insights/analyzers/anomaly_detector.py
-----------------------------------------------
SCRUM-25 v2.0 - Per-product DBSCAN + stream correlation + 12-month rolling baseline
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, stdev

from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncDate

from apps.ai_insights.client import AIClient, AIClientError

logger = logging.getLogger(__name__)

DETECTION_WINDOW_DAYS = 365
MIN_BASELINE_POINTS   = 5
EXCLUSION_WINDOW      = 30
Z_SCORE_MEDIUM        = 2.0
Z_SCORE_HIGH          = 2.5
Z_SCORE_CRITICAL      = 3.5
AI_MAX_ANOMALIES      = 4
AI_INTER_CALL_DELAY   = 2
TOP_PRODUCTS_N        = 20
BASELINE_WEEKS        = 52

SYSTEM_PROMPT = """You are a senior business intelligence analyst for WEEG, a BI platform for Libyan distribution companies.

Given a statistical anomaly, identify root causes and recommend actions.
Return ONLY valid JSON:
{
  "ai_explanation": "<2-3 sentences>",
  "likely_causes": ["<cause 1>", "<cause 2>"],
  "business_impact": "<quantified impact>",
  "recommended_actions": ["<action 1>", "<action 2>"],
  "confidence": "high" | "medium" | "low"
}"""


class AnomalyDetector:

    def __init__(self):
        self._client = AIClient()

    def detect(self, company, use_ai: bool = True) -> dict:
        logger.info("[AnomalyDetector] Starting for company=%s", company.id)
        streams   = self._build_time_series(company)
        anomalies = []

        for stream_name, series in streams.items():
            anomalies.extend(self._detect_in_stream(stream_name, series))

        anomalies.extend(self._detect_per_product(company))
        anomalies = self._correlate_streams(anomalies, streams)

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        anomalies.sort(key=lambda a: (severity_order.get(a["severity"], 4), -abs(a["z_score"])))

        if use_ai:
            import time as _time
            candidates = [a for a in anomalies if a["severity"] in ("critical", "high")][:AI_MAX_ANOMALIES]
            for i, anomaly in enumerate(candidates):
                if i > 0:
                    _time.sleep(AI_INTER_CALL_DELAY)
                try:
                    ai_result = self._call_ai(anomaly, company.id)
                    if ai_result and not ai_result.get("error"):
                        idx = next(j for j, a in enumerate(anomalies)
                                   if a["stream"] == anomaly["stream"] and a["date"] == anomaly["date"])
                        anomalies[idx].update({
                            "ai_explanation":      ai_result.get("ai_explanation", ""),
                            "likely_causes":       ai_result.get("likely_causes", []),
                            "business_impact":     ai_result.get("business_impact", ""),
                            "recommended_actions": ai_result.get("recommended_actions", []),
                            "confidence":          ai_result.get("confidence", "medium"),
                        })
                except AIClientError as exc:
                    logger.warning("[AnomalyDetector] AI failed: %s", exc)

        summary = {
            "total":    len(anomalies),
            "critical": sum(1 for a in anomalies if a["severity"] == "critical"),
            "high":     sum(1 for a in anomalies if a["severity"] == "high"),
            "medium":   sum(1 for a in anomalies if a["severity"] == "medium"),
            "low":      sum(1 for a in anomalies if a["severity"] == "low"),
        }
        logger.info("[AnomalyDetector] Detected %d anomalies for company=%s", len(anomalies), company.id)
        return {
            "detection_window_days": DETECTION_WINDOW_DAYS,
            "baseline_weeks":        BASELINE_WEEKS,
            "summary":               summary,
            "anomalies":             anomalies,
        }

    def _build_time_series(self, company) -> dict:
        from apps.transactions.models import MaterialMovement
        today      = date.today()
        full_start = today - timedelta(days=DETECTION_WINDOW_DAYS)
        base_qs    = (
            MaterialMovement.objects
            .filter(company=company, movement_type="ف بيع", movement_date__gte=full_start)
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
        )
        daily_revenue   = dict(
            base_qs.annotate(day=TruncDate("movement_date"))
            .values("day").annotate(value=Sum("total_out")).values_list("day", "value")
        )
        daily_txns      = dict(
            base_qs.annotate(day=TruncDate("movement_date"))
            .values("day").annotate(value=Count("id")).values_list("day", "value")
        )
        daily_customers = {
            row["day"]: row["value"]
            for row in base_qs.annotate(day=TruncDate("movement_date"))
            .values("day").annotate(value=Count("customer_name", distinct=True))
        }
        streams = {"daily_revenue_lyd": [], "daily_transactions": [], "daily_unique_customers": []}
        all_days = sorted(daily_revenue.keys() | daily_txns.keys() | daily_customers.keys())
        for day in all_days:
            day_obj = day.date() if hasattr(day, "date") else day
            for sk, dm in [("daily_revenue_lyd", daily_revenue),
                           ("daily_transactions", daily_txns),
                           ("daily_unique_customers", daily_customers)]:
                val = dm.get(day, dm.get(day_obj, 0))
                streams[sk].append({"date": str(day_obj), "value": float(val or 0)})
        return streams

    def _detect_in_stream(self, stream_name: str, series: list) -> list:
        if len(series) < MIN_BASELINE_POINTS + 1:
            return []
        anomalies = []
        for point in series:
            val = point["value"]
            if val == 0:
                continue
            target_date   = date.fromisoformat(point["date"])
            baseline_vals = [
                p["value"] for p in series
                if p["value"] > 0
                and abs((date.fromisoformat(p["date"]) - target_date).days) > EXCLUSION_WINDOW
            ]
            if len(baseline_vals) < MIN_BASELINE_POINTS:
                continue
            mu = mean(baseline_vals)
            if len(baseline_vals) < 2:
                continue
            sd = stdev(baseline_vals)
            if sd < 1e-9:
                continue
            z_score = (val - mu) / sd
            abs_z   = abs(z_score)
            if abs_z < Z_SCORE_MEDIUM:
                continue
            direction = "spike" if z_score > 0 else "drop"
            severity  = ("critical" if abs_z >= Z_SCORE_CRITICAL else
                         "high"     if abs_z >= Z_SCORE_HIGH     else "medium")
            anomalies.append({
                "stream":              stream_name,
                "date":                point["date"],
                "observed_value":      round(val, 2),
                "expected_value":      round(mu, 2),
                "z_score":             round(z_score, 3),
                "deviation_pct":       round((val - mu) / mu * 100, 1) if mu > 0 else 0.0,
                "direction":           direction,
                "severity":            severity,
                "anomaly_type":        "one_off",
                "baseline_mean":       round(mu, 2),
                "baseline_std":        round(sd, 2),
                "correlated_streams":  [],
                "ai_explanation":      self._default_explanation(stream_name, direction, z_score, val, mu),
                "likely_causes":       self._default_causes(stream_name, direction),
                "business_impact":     self._default_impact(stream_name, direction, val, mu),
                "recommended_actions": self._default_actions(stream_name, direction, val, mu),
                "confidence":          "medium",
            })
        return anomalies

    def _detect_per_product(self, company) -> list:
        """Detect revenue anomalies per top-N SKU using same rolling 3-sigma."""
        from apps.transactions.models import MaterialMovement
        today      = date.today()
        full_start = today - timedelta(days=DETECTION_WINDOW_DAYS)

        top_products = (
            MaterialMovement.objects
            .filter(company=company, movement_type="ف بيع", movement_date__gte=full_start)
            .exclude(Q(material_code__isnull=True) | Q(material_code=""))
            .values("material_code", "material_name")
            .annotate(total_rev=Sum("total_out"))
            .order_by("-total_rev")[:TOP_PRODUCTS_N]
        )
        product_codes = [p["material_code"] for p in top_products]
        name_map      = {p["material_code"]: (p["material_name"] or p["material_code"])[:40]
                         for p in top_products}

        daily_per_product = defaultdict(list)
        for row in (
            MaterialMovement.objects
            .filter(company=company, movement_type="ف بيع",
                    movement_date__gte=full_start, material_code__in=product_codes)
            .annotate(day=TruncDate("movement_date"))
            .values("material_code", "day")
            .annotate(value=Sum("total_out"))
            .order_by("material_code", "day")
        ):
            day_obj = row["day"].date() if hasattr(row["day"], "date") else row["day"]
            daily_per_product[row["material_code"]].append({
                "date": str(day_obj), "value": float(row["value"] or 0)
            })

        anomalies = []
        for code, series in daily_per_product.items():
            product_name = name_map.get(code, code)
            for a in self._detect_in_stream(f"product:{code}", series):
                a["product_code"]  = code
                a["product_name"]  = product_name
                a["anomaly_type"]  = "product_level"
                a["stream"]        = f"product_revenue:{product_name}"
                anomalies.append(a)

        logger.info("[AnomalyDetector] Product-level: %d anomalies", len(anomalies))
        return anomalies

    def _correlate_streams(self, anomalies: list, streams: dict) -> list:
        """Upgrade confidence when multiple streams are anomalous on the same day."""
        anomaly_index = defaultdict(set)
        for a in anomalies:
            anomaly_index[a["date"]].add(a["stream"])

        for anomaly in anomalies:
            day          = anomaly["date"]
            this_stream  = anomaly["stream"]
            direction    = anomaly["direction"]
            others       = [s for s in anomaly_index[day] if s != this_stream]
            anomaly["correlated_streams"] = others

            if not others:
                continue
            # Revenue drop + customers drop → external event
            if (this_stream == "daily_revenue_lyd" and direction == "drop"
                    and "daily_unique_customers" in others):
                anomaly["confidence"] = "high"
                anomaly["likely_causes"] = [
                    "External event affecting the area (logistics, disruption, holiday)",
                    "Market closure or public event reducing business activity",
                ]
            # Revenue drop only → data entry suspicion
            elif this_stream == "daily_revenue_lyd" and direction == "drop" and not others:
                anomaly["likely_causes"] = [
                    "Possible missing data import for this date",
                    "Sudden loss of orders from one or two key customers",
                ]
            # Revenue spike + customers spike → genuine demand surge
            elif (this_stream == "daily_revenue_lyd" and direction == "spike"
                  and "daily_unique_customers" in others):
                anomaly["confidence"] = "high"
                anomaly["likely_causes"] = [
                    "Promotional campaign or bulk event generating multi-customer demand",
                    "End-of-period rush buying ahead of price or availability change",
                ]
        return anomalies

    def _call_ai(self, anomaly: dict, company_id) -> dict | None:
        ctx = ""
        if anomaly.get("correlated_streams"):
            ctx = f"\nCorrelated streams on this date: {', '.join(anomaly['correlated_streams'])}"
        user_prompt = (
            f"Anomaly in: {anomaly['stream']}\n"
            f"Date: {anomaly['date']} | Direction: {anomaly['direction']} "
            f"({anomaly['deviation_pct']:+.1f}%)\n"
            f"Observed: {anomaly['observed_value']:,.2f} | Expected: {anomaly['expected_value']:,.2f}\n"
            f"Z-score: {anomaly['z_score']:.3f} | Severity: {anomaly['severity']}{ctx}\n"
            f"Context: Libyan B2B distribution company."
        )
        return self._client.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            model="smart", max_tokens=500,
            analyzer="anomaly_detector", company_id=str(company_id),
        )

    @staticmethod
    def _default_explanation(stream, direction, z, val, mu) -> str:
        pct = abs((val - mu) / mu * 100) if mu > 0 else 0
        return (
            f"The {stream.replace('_', ' ')} was {pct:.0f}% "
            f"{'above' if direction == 'spike' else 'below'} "
            f"the 12-month rolling baseline ({mu:,.0f}), a {abs(z):.1f}-sigma deviation."
        )

    @staticmethod
    def _default_causes(stream, direction) -> list:
        if direction == "spike":
            return (["Large one-off order", "Bulk pre-purchase ahead of price increase"]
                    if "revenue" in stream else ["Promotional event", "End-of-month rush"])
        return (["Key customer absent — possible competitor switch", "Stock-out blocking orders"]
                if "revenue" in stream else ["Public holiday", "Logistics disruption"])

    @staticmethod
    def _default_impact(stream, direction, val, mu) -> str:
        diff = abs(val - mu)
        if direction == "drop" and "revenue" in stream:
            return f"Shortfall {diff:,.0f} LYD. Monthly impact if recurring: {diff*30:,.0f} LYD."
        return f"Deviation of {diff:,.0f} units from expected baseline."

    @staticmethod
    def _default_actions(stream, direction, val, mu) -> list:
        if direction == "drop" and "revenue" in stream:
            return ["Check stock availability for top 5 products on this date.",
                    "Contact top 3 customers to identify delayed or cancelled orders."]
        return [f"Verify {stream.replace('_', ' ')} data accuracy for this date.",
                "Escalate to relevant department for root cause investigation."]