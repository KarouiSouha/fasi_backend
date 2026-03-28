"""
apps/ai_insights/analyzers/predictor.py
-----------------------------------------
SCRUM-30 v2.0 - Holt-Winters + Monte Carlo confidence intervals
"""

import logging
import math
import random
from datetime import date, timedelta
from collections import defaultdict

from django.db.models import Count, Sum, Q
from django.db.models.functions import TruncMonth

from apps.ai_insights.client import AIClient, AIClientError

logger = logging.getLogger(__name__)

HISTORY_MONTHS    = 12
FORECAST_MONTHS   = 3
MIN_HISTORY       = 6
MONTE_CARLO_RUNS  = 1000  # simulations for confidence intervals

MONTH_NAMES = {1:"January",2:"February",3:"March",4:"April",5:"May",6:"June",
               7:"July",8:"August",9:"September",10:"October",11:"November",12:"December"}

SYSTEM_PROMPT = """You are a senior financial planning analyst for WEEG, a BI platform for Libyan distribution companies.

You receive a 3-month revenue forecast built with Holt-Winters exponential smoothing
and Monte Carlo P10/P50/P90 confidence intervals.
Return ONLY valid JSON:
{
  "forecast_narrative": "<3-4 sentences>",
  "primary_risk":       "<biggest threat>",
  "growth_opportunity": "<biggest upside>",
  "recommendations": [{"month_target": "<month>", "action": "<action>", "owner": "<role>", "expected_impact_lyd": <float>}],
  "cash_flow_alert": "<cash flow warning if any>",
  "confidence": "high" | "medium" | "low"
}"""


class Predictor:

    def __init__(self):
        self._client = AIClient()

    def predict(self, company, use_ai: bool = True) -> dict:
        logger.info("[Predictor] Starting for company=%s", company.id)
        history = self._fetch_monthly_history(company)
        if len(history) < MIN_HISTORY:
            return {"error": "Insufficient data for forecasting.", "revenue_forecast": [], "confidence": "low"}

        seasonality = self._compute_seasonality_indices(history)

        # v2.0: Holt-Winters instead of linear regression
        hw_model  = self._fit_holt_winters(history)
        forecast  = self._generate_forecast_hw(hw_model, seasonality, history)

        # v2.0: Monte Carlo confidence intervals
        forecast  = self._add_monte_carlo_ci(forecast, hw_model, history)

        customer_forecast  = self._forecast_customers(company)
        cash_flow_forecast = self._forecast_cash_flow(company, forecast)

        ai_result = None
        if use_ai:
            try:
                ai_result = self._call_ai(history, forecast, hw_model, company.id)
            except AIClientError as exc:
                logger.warning("[Predictor] AI unavailable: %s", exc)

        return self._format_result(history, hw_model, seasonality, forecast,
                                    customer_forecast, cash_flow_forecast, ai_result)

    # ── History fetch ─────────────────────────────────────────────────────────

    def _fetch_monthly_history(self, company) -> list:
        from apps.transactions.models import MaterialMovement
        today      = date.today()
        start_date = today.replace(day=1) - timedelta(days=HISTORY_MONTHS * 31)
        rows = (
            MaterialMovement.objects
            .filter(company=company, movement_type="ف بيع", movement_date__gte=start_date)
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .annotate(month=TruncMonth("movement_date"))
            .values("month")
            .annotate(revenue=Sum("total_out"), transaction_count=Count("id"),
                      unique_customers=Count("customer_name", distinct=True))
            .order_by("month")
        )
        return [{"t": i, "year": r["month"].year, "month": r["month"].month,
                 "period": f"{MONTH_NAMES[r['month'].month]} {r['month'].year}",
                 "revenue_lyd": float(r["revenue"] or 0),
                 "transaction_count": r["transaction_count"] or 0,
                 "unique_customers": r["unique_customers"] or 0}
                for i, r in enumerate(rows)]

    # ── Seasonality ───────────────────────────────────────────────────────────

    @staticmethod
    def _compute_seasonality_indices(history: list) -> dict:
        by_month = defaultdict(list)
        for row in history:
            by_month[row["month"]].append(row["revenue_lyd"])
        all_vals    = [v for vs in by_month.values() for v in vs]
        overall_avg = sum(all_vals) / len(all_vals) if all_vals else 1.0
        indices = {}
        for m in range(1, 13):
            vals = by_month.get(m, [])
            indices[m] = round(sum(vals) / len(vals) / overall_avg, 4) if vals and overall_avg > 0 else 1.0
        return indices

    # ── Holt-Winters (additive seasonality) ──────────────────────────────────

    @staticmethod
    def _fit_holt_winters(history: list, alpha: float = 0.3, beta: float = 0.1,
                           gamma: float = 0.3) -> dict:
        """
        Triple Exponential Smoothing (Holt-Winters additive).
        α = level smoothing, β = trend smoothing, γ = seasonal smoothing
        Season period = 12 months.
        """
        n       = len(history)
        period  = min(12, n)
        y       = [row["revenue_lyd"] for row in history]

        # Initialise level and trend
        level = sum(y[:period]) / period
        trend = (sum(y[period:period*2][:period]) / period - level) / period if n >= period * 2 else 0.0

        # Initialise seasonal components (additive)
        season = [y[i] - level for i in range(period)]

        levels   = [level]
        trends   = [trend]
        seasons  = list(season)
        fitted   = []
        residuals = []

        for i in range(n):
            if i == 0:
                fitted.append(level + trend + seasons[i % period])
            else:
                s_prev = seasons[(i - period) % len(seasons)] if i >= period else seasons[i % period]
                l_prev = levels[-1]
                t_prev = trends[-1]
                l_new  = alpha * (y[i] - s_prev) + (1 - alpha) * (l_prev + t_prev)
                t_new  = beta  * (l_new - l_prev) + (1 - beta) * t_prev
                s_new  = gamma * (y[i] - l_new) + (1 - gamma) * s_prev
                levels.append(l_new)
                trends.append(t_new)
                seasons.append(s_new)
                fitted.append(l_new + t_new + s_new)

            residuals.append(y[i] - fitted[-1])

        residual_std = math.sqrt(sum(r ** 2 for r in residuals) / max(1, n - 1))
        mape = sum(abs(r) / max(1, y[i]) for i, r in enumerate(residuals)) / n * 100

        return {
            "level":          levels[-1],
            "trend":          trends[-1],
            "seasons":        seasons[-period:] if len(seasons) >= period else seasons,
            "residual_std":   round(residual_std, 2),
            "mape":           round(mape, 2),
            "alpha":          alpha, "beta": beta, "gamma": gamma,
            "period":         period,
            "fitted":         fitted,
            "residuals":      residuals,
            "direction":      ("growing"  if trends[-1] > 0.001 else
                               "declining" if trends[-1] < -0.001 else "stable"),
            "slope_pct":      round(trends[-1] / max(1, levels[-1]) * 100, 3),
        }

    # ── Forecast generation ───────────────────────────────────────────────────

    def _generate_forecast_hw(self, model: dict, seasonality: dict, history: list) -> list:
        today      = date.today()
        base_month = today.month
        base_year  = today.year
        level      = model["level"]
        trend      = model["trend"]
        seasons    = model["seasons"]
        period     = model["period"]

        forecasts = []
        for i in range(1, FORECAST_MONTHS + 1):
            target_month = (base_month + i - 1) % 12 + 1
            target_year  = base_year + (base_month + i - 1) // 12
            season_idx   = (len(history) + i - 1) % period
            season_comp  = seasons[season_idx] if season_idx < len(seasons) else 0

            base_forecast = max(0, level + trend * i + season_comp)
            si            = seasonality.get(target_month, 1.0)

            forecasts.append({
                "month": target_month, "year": target_year,
                "period": f"{MONTH_NAMES[target_month]} {target_year}",
                "base_lyd":          round(base_forecast, 2),
                "seasonality_index": si,
                "trend_component":   round(level + trend * i, 2),
                # Monte Carlo fields added later
                "p10_lyd": 0.0, "p50_lyd": 0.0, "p90_lyd": 0.0,
                "optimistic_lyd":  round(base_forecast * 1.15, 2),
                "pessimistic_lyd": round(max(0, base_forecast * 0.85), 2),
                "upside_pct":  15.0, "downside_pct": 15.0,
            })
        return forecasts

    # ── Monte Carlo confidence intervals ──────────────────────────────────────
    @staticmethod
    def _add_monte_carlo_ci(forecast: list, model: dict, history: list) -> list:
        """
        Monte Carlo P10/P50/P90.
        Garantit une variance minimale même si les résidus sont proches de zéro
        (datasets récents ou très réguliers), sinon P10=P50=P90.
        """
        residuals = model.get("residuals", [])
        if not residuals:
            return forecast

        # Variance minimale : 5% de la moyenne des valeurs prévues
        avg_base = sum(fm["base_lyd"] for fm in forecast) / max(1, len(forecast))
        min_std = avg_base * 0.05

        residual_std = model.get("residual_std", 0)
        effective_std = max(residual_std, min_std)

        # Si les résidus réels sont trop petits, on les amplifie proportionnellement
        if residual_std > 0 and residual_std < min_std:
            scale = min_std / residual_std
            residuals = [r * scale for r in residuals]

        simulations = [[0.0] * MONTE_CARLO_RUNS for _ in range(len(forecast))]
        for run in range(MONTE_CARLO_RUNS):
            for j, fm in enumerate(forecast):
                noise = random.choice(residuals)
                simulations[j][run] = max(0, fm["base_lyd"] + noise)

        for j, fm in enumerate(forecast):
            col = sorted(simulations[j])
            p10 = col[int(MONTE_CARLO_RUNS * 0.10)]
            p50 = col[int(MONTE_CARLO_RUNS * 0.50)]
            p90 = col[int(MONTE_CARLO_RUNS * 0.90)]

            # Forcer un spread minimum visible (±3% au moins)
            if p90 - p10 < fm["base_lyd"] * 0.06:
                spread = fm["base_lyd"] * 0.03
                p10 = max(0, fm["base_lyd"] - spread)
                p90 = fm["base_lyd"] + spread
                p50 = fm["base_lyd"]

            fm["p10_lyd"]         = round(p10, 2)
            fm["p50_lyd"]         = round(p50, 2)
            fm["p90_lyd"]         = round(p90, 2)
            fm["pessimistic_lyd"] = round(p10, 2)
            fm["optimistic_lyd"]  = round(p90, 2)
            base = fm["base_lyd"]
            fm["upside_pct"]   = round((p90 - base) / base * 100, 1) if base > 0 else 0
            fm["downside_pct"] = round((base - p10) / base * 100, 1) if base > 0 else 0

        return forecast
    # ── Customer forecast ─────────────────────────────────────────────────────

    def _forecast_customers(self, company) -> list:
        from apps.transactions.models import MaterialMovement
        today = date.today()
        rows = (
            MaterialMovement.objects
            .filter(company=company, movement_type="ف بيع",
                    movement_date__gte=today - timedelta(days=90))
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .annotate(month=TruncMonth("movement_date"))
            .values("month").annotate(unique_customers=Count("customer_name", distinct=True))
            .order_by("month")
        )
        vals = [float(r["unique_customers"]) for r in rows]
        if len(vals) < 2:
            return []
        trend_pm   = (vals[-1] - vals[0]) / max(1, len(vals) - 1)
        base_month = today.month; base_year = today.year
        return [{"period": f"{MONTH_NAMES[(base_month + i - 1) % 12 + 1]} {base_year + (base_month + i - 1) // 12}",
                 "projected_active_customers": round(max(0, vals[-1] + trend_pm * i)),
                 "trend_per_month": round(trend_pm, 1)}
                for i in range(1, FORECAST_MONTHS + 1)]

    # ── Cash flow forecast ────────────────────────────────────────────────────

    def _forecast_cash_flow(self, company, revenue_forecast: list) -> dict:
        from apps.aging.models import AgingReceivable, AgingSnapshot
        latest_snap = (AgingSnapshot.objects.filter(company=company)
                       .order_by("-uploaded_at").first())
        if not latest_snap:
            return {}
        from django.db.models import Sum as DSum
        ag = AgingReceivable.objects.filter(snapshot=latest_snap).aggregate(
            total=DSum("total"), current=DSum("current")
        )
        total_rec    = float(ag["total"]   or 0)
        total_curr   = float(ag["current"] or 0)
        total_overdue = max(0.0, total_rec - total_curr)
        collection_rate = 1 - (total_overdue / total_rec) if total_rec > 0 else 0.70
        return {
            "current_receivable_lyd": round(total_rec, 2),
            "current_overdue_lyd":    round(total_overdue, 2),
            "collection_rate_pct":    round(collection_rate * 100, 1),
            "monthly_projections": [{
                "period": fm["period"],
                "expected_revenue_lyd": fm["base_lyd"],
                "expected_cash_collected_lyd": round(fm["base_lyd"] * collection_rate, 2),
                "collection_rate_pct": round(collection_rate * 100, 1),
                "collection_gap_lyd": round(fm["base_lyd"] * (1 - collection_rate), 2),
            } for fm in revenue_forecast],
        }

    # ── AI call ───────────────────────────────────────────────────────────────

    def _call_ai(self, history, forecast, model, company_id) -> dict | None:
        hist_lines = [f"  {r['period']:<20} {r['revenue_lyd']:>14,.0f} LYD" for r in history[-6:]]
        fc_lines   = [
            f"  {fm['period']:<20} base={fm['base_lyd']:>12,.0f}  "
            f"P10={fm['p10_lyd']:>12,.0f}  P90={fm['p90_lyd']:>12,.0f}"
            for fm in forecast
        ]
        user_prompt = (
            f"Revenue Forecast — Libyan B2B Distribution\n"
            f"Model: Holt-Winters (α={model['alpha']}, β={model['beta']}, γ={model['gamma']})\n"
            f"MAPE: {model['mape']:.1f}% | Trend: {model['direction']} "
            f"({model['slope_pct']:+.2f}%/mo)\n\n"
            f"Recent history (last 6 months):\n" + "\n".join(hist_lines) + "\n\n"
            f"3-Month Forecast (Monte Carlo P10/P50/P90):\n" + "\n".join(fc_lines) + "\n\n"
            f"Base total: {sum(fm['base_lyd'] for fm in forecast):,.0f} LYD | "
            f"P90 total: {sum(fm['p90_lyd'] for fm in forecast):,.0f} LYD"
        )
        return self._client.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            model="smart", max_tokens=800,
            analyzer="predictor", company_id=str(company_id),
        )

    # ── Output ────────────────────────────────────────────────────────────────

    def _format_result(self, history, model, seasonality, forecast,
                        customer_forecast, cash_flow, ai_result) -> dict:
        narrative = (
            f"Revenue trend is {model['direction']} "
            f"(Holt-Winters MAPE={model['mape']:.1f}%). "
            f"Base-case 3-month forecast: {sum(fm['base_lyd'] for fm in forecast):,.0f} LYD. "
            f"Monte Carlo P90: {sum(fm['p90_lyd'] for fm in forecast):,.0f} LYD."
        )
        rec = [{"month_target": forecast[0]["period"] if forecast else "Next month",
                "action": "Accelerate collections and restock Class A items for peak demand.",
                "owner": "Operations Manager", "expected_impact_lyd": 0}]
        primary_risk = (
            f"Declining trend ({model['slope_pct']:+.2f}%/mo) may accelerate."
            if model["direction"] == "declining"
            else "Residual uncertainty spans P10-P90; major shocks not modeled."
        )
        confidence = "high" if model["mape"] < 10 else "medium" if model["mape"] < 20 else "low"

        if ai_result and not ai_result.get("error"):
            narrative    = ai_result.get("forecast_narrative", narrative)
            rec          = ai_result.get("recommendations",    rec)
            primary_risk = ai_result.get("primary_risk",       primary_risk)
            confidence   = ai_result.get("confidence",         confidence)

        return {
            "forecast_months": FORECAST_MONTHS,
            "history_months_used": len(history),
            "model_type": "holt_winters",
            "trend_model": {
                "direction": model["direction"], "slope_pct": model["slope_pct"],
                "mape": model["mape"], "residual_std": model["residual_std"],
                "alpha": model["alpha"], "beta": model["beta"], "gamma": model["gamma"],
                # Compat fields for frontend
                "r_squared": 0.0, "avg_revenue": history[-1]["revenue_lyd"] if history else 0,
                "last_t": len(history),
            },
            "monte_carlo_runs": MONTE_CARLO_RUNS,
            "revenue_forecast":              forecast,
            "forecast_total_base_lyd":       round(sum(fm["base_lyd"] for fm in forecast), 2),
            "forecast_total_optimistic_lyd": round(sum(fm["p90_lyd"] for fm in forecast), 2),
            "forecast_total_pessimistic_lyd":round(sum(fm["p10_lyd"] for fm in forecast), 2),
            "customer_forecast":             customer_forecast,
            "cash_flow_forecast":            cash_flow,
            "forecast_narrative":            narrative,
            "primary_risk":                  primary_risk,
            "recommendations":               rec,
            "confidence":                    confidence,
        }