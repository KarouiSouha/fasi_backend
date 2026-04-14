"""
apps/ai_insights/analyzers/seasonal_analyzer.py
-------------------------------------------------
SCRUM-26 v3.0 — 12-month window + smart Ramadan + exchange rate + current-season-only

Changes vs v2.0:
  1. HISTORY_MONTHS 24 → 12  (année courante uniquement)
  2. Ramadan : affiché uniquement si en cours ou dans les 60 prochains jours
  3. Taux de change LYD/USD intégré dans les prompts et la réponse
  4. Recommandations saisonnières filtrées : uniquement saisons à venir ou en cours
"""

import logging
import math
from collections import defaultdict
from datetime import date, timedelta

from django.db.models import Sum, Q
from django.db.models.functions import TruncMonth

from apps.ai_insights.client import AIClient, AIClientError

logger = logging.getLogger(__name__)

HISTORY_MONTHS    = 12           # ← Changed from 24: current year only
MIN_MONTHS        = 6
PEAK_THRESHOLD    = 1.15
TROUGH_THRESHOLD  = 0.85
RAMADAN_LOOKAHEAD = 60           # Show Ramadan insight ≤ 60 days before start
MONTH_NAMES = {
    1: "January",  2: "February", 3: "March",    4: "April",
    5: "May",      6: "June",     7: "July",      8: "August",
    9: "September",10: "October", 11: "November", 12: "December",
}

# ── Ramadan dates (Gregorian start, approximate) ──────────────────────────────
RAMADAN_STARTS = {
    2022: date(2022, 4,  2),
    2023: date(2023, 3, 22),
    2024: date(2024, 3, 11),
    2025: date(2025, 3,  1),
    2026: date(2026, 2, 18),
    2027: date(2027, 2,  8),
    2028: date(2028, 1, 28),
}
RAMADAN_DURATION_DAYS = 30

# ── Eid al-Adha dates (approximate) ──────────────────────────────────────────
EID_AL_ADHA = {
    2023: date(2023, 6, 28), 2024: date(2024, 6, 16),
    2025: date(2025, 6,  6), 2026: date(2026, 5, 27),
}

SYSTEM_PROMPT = """You are a supply chain & demand planning expert for WEEG, a BI platform for \
Libyan distribution companies.

You receive monthly seasonality indices and Islamic calendar event flags.
Currency context is provided — include the LYD/USD exchange rate in your analysis \
when discussing financial impacts.

Return ONLY valid JSON:
{
  "seasonal_narrative": "<3-4 sentences>",
  "peak_season_story":  "<what drives peak demand>",
  "trough_season_story": "<what causes trough>",
  "stock_preparation_calendar": [
    {"month": "<name>", "action": "<action>", "lead_time_weeks": <int>, "rationale": "<why>"}
  ],
  "staffing_implications": "<quantified staffing impact>",
  "ai_recommendations": ["<rec 1>", "<rec 2>", "<rec 3>"],
  "confidence": "high" | "medium" | "low"
}"""


# ── Exchange rate helper ──────────────────────────────────────────────────────

def _get_exchange_rate() -> dict:
    """
    Returns LYD/USD exchange rate.
    Configure LYD_USD_RATE in Django settings for an accurate rate.
    Falls back to a reasonable default (Central Bank of Libya official rate).
    """
    from django.conf import settings
    rate_setting = getattr(settings, "LYD_USD_RATE", None)
    if rate_setting:
        return {
            "usd_to_lyd": float(rate_setting),
            "lyd_to_usd": round(1 / float(rate_setting), 4),
            "source": "settings",
        }
    # Default: ~4.85 LYD per 1 USD (Central Bank of Libya official peg, 2024-2025)
    default_rate = 4.85
    return {
        "usd_to_lyd": default_rate,
        "lyd_to_usd": round(1 / default_rate, 4),
        "source": "default",
        "note": "Set LYD_USD_RATE in Django settings for an accurate live rate.",
    }


# ── Ramadan relevance check ───────────────────────────────────────────────────

def _get_relevant_ramadan(today: date) -> dict | None:
    """
    Returns Ramadan info only if:
      - Ramadan is currently ongoing, OR
      - Ramadan starts within the next RAMADAN_LOOKAHEAD days.

    Returns None for any Ramadan that is fully in the past
    (even if it was this year — e.g. if today > Ramadan end, ignore it).
    """
    for year in [today.year, today.year + 1]:
        start = RAMADAN_STARTS.get(year)
        if not start:
            continue
        end = start + timedelta(days=RAMADAN_DURATION_DAYS)

        if start <= today <= end:
            days_remaining = (end - today).days
            return {
                "status": "ongoing",
                "start": str(start),
                "end": str(end),
                "days_remaining": days_remaining,
                "label": f"Ramadan {year} is ongoing ({days_remaining} days left)",
            }

        days_until = (start - today).days
        if 0 < days_until <= RAMADAN_LOOKAHEAD:
            return {
                "status": "upcoming",
                "start": str(start),
                "end": str(end),
                "days_until": days_until,
                "label": f"Ramadan {year} in {days_until} days — prepare now",
            }

    return None  # Ramadan is either past or too far in the future — ignore


class SeasonalAnalyzer:

    def __init__(self):
        self._client = AIClient()

    def analyze(self, company, use_ai: bool = True) -> dict:
        logger.info("[SeasonalAnalyzer] Starting for company=%s", company.id)
        monthly_series = self._build_monthly_series(company)
        if len(monthly_series) < MIN_MONTHS:
            return self._empty_result(
                "Insufficient historical data (minimum 6 months required)."
            )

        exchange_rate     = _get_exchange_rate()
        today             = date.today()
        relevant_ramadan  = _get_relevant_ramadan(today)

        # STL-inspired decomposition (12-month window)
        detrended      = self._remove_trend_stl(monthly_series)
        indices        = self._compute_seasonality_indices(detrended, monthly_series)
        trend          = self._compute_trend(monthly_series)
        peaks, troughs = self._classify_months(indices)

        # Ramadan effect (historical data, only if relevant Ramadan exists)
        ramadan_analysis = self._detect_ramadan_effect(
            company, monthly_series, relevant_ramadan
        )

        category_pats  = self._compute_category_patterns(company)
        upcoming_alert = self._check_upcoming_peak(peaks, today)
        current_season = self._current_season_label(indices, today)

        ai_result = None
        if use_ai:
            try:
                ai_result = self._call_ai(
                    indices, peaks, troughs, trend,
                    ramadan_analysis, relevant_ramadan,
                    exchange_rate, company.id,
                )
            except AIClientError as exc:
                logger.warning("[SeasonalAnalyzer] AI unavailable: %s", exc)

        return self._format_result(
            indices, trend, peaks, troughs, category_pats,
            upcoming_alert, current_season, ramadan_analysis,
            relevant_ramadan, exchange_rate, ai_result, today,
        )

    # ── Monthly series ────────────────────────────────────────────────────────

    def _build_monthly_series(self, company) -> list:
        from apps.transactions.models import MaterialMovement
        today      = date.today()
        # 12-month rolling window (current year only)
        start_date = today.replace(day=1) - timedelta(days=HISTORY_MONTHS * 31)
        rows = (
            MaterialMovement.objects
            .filter(company=company, movement_type="ف بيع", movement_date__gte=start_date)
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .annotate(month=TruncMonth("movement_date"))
            .values("month").annotate(revenue=Sum("total_out"))
            .order_by("month")
        )
        return [
            {
                "year": r["month"].year, "month": r["month"].month,
                "revenue": float(r["revenue"] or 0),
            }
            for r in rows
        ]

    # ── STL-inspired decomposition (12-month CMA) ─────────────────────────────

    @staticmethod
    def _remove_trend_stl(series: list) -> list:
        """
        Trend removal via centred 12-month moving average.
        Isolates seasonal + irregular component before computing SI.
        """
        n      = len(series)
        window = 12
        cma    = []
        for i in range(n):
            half = window // 2
            lo   = max(0, i - half)
            hi   = min(n, i + half + 1)
            vals = [p["revenue"] for p in series[lo:hi] if p["revenue"] > 0]
            cma.append(sum(vals) / len(vals) if vals else series[i]["revenue"])

        detrended = []
        for i, row in enumerate(series):
            trend_val = cma[i] if cma[i] > 0 else 1.0
            ratio     = row["revenue"] / trend_val if trend_val > 0 else 1.0
            detrended.append({**row, "detrended": ratio})
        return detrended

    @staticmethod
    def _compute_seasonality_indices(detrended: list, raw: list) -> dict:
        by_month: dict[int, list] = defaultdict(list)
        for row in detrended:
            if row["detrended"] > 0:
                by_month[row["month"]].append(row["detrended"])

        overall_avg = sum(r["detrended"] for r in detrended if r["detrended"] > 0)
        n_valid     = sum(1 for r in detrended if r["detrended"] > 0)
        overall_avg = overall_avg / n_valid if n_valid > 0 else 1.0

        raw_avg_by_month: dict[int, list] = defaultdict(list)
        for r in raw:
            raw_avg_by_month[r["month"]].append(r["revenue"])

        indices = {}
        for month_num in range(1, 13):
            vals = by_month.get(month_num, [])
            si   = (
                (sum(vals) / len(vals)) / overall_avg
                if vals and overall_avg > 0
                else None
            )
            raw_vals  = raw_avg_by_month.get(month_num, [])
            month_avg = sum(raw_vals) / len(raw_vals) if raw_vals else 0.0
            indices[month_num] = {
                "month_num":                month_num,
                "month_name":               MONTH_NAMES[month_num],
                "seasonality_index":        round(si, 4) if si is not None else None,
                "avg_monthly_revenue_lyd":  round(month_avg, 2),
                "data_points":              len(vals),
                "label": (
                    "peak"   if si and si >= PEAK_THRESHOLD   else
                    "trough" if si and si <= TROUGH_THRESHOLD else
                    "normal" if si else "no_data"
                ),
            }
        return indices

    # ── Ramadan effect (only when relevant) ──────────────────────────────────

    def _detect_ramadan_effect(
        self,
        company,
        series: list,
        relevant_ramadan: dict | None,
    ) -> dict:
        """
        Calculates historical Ramadan sales effect.
        Only included in output when Ramadan is ongoing or upcoming (≤60 days).
        Returns empty/disabled dict when Ramadan is past or too far.
        """
        if not relevant_ramadan:
            return {
                "detected":    False,
                "relevant":    False,
                "status":      "not_relevant",
                "note":        "Ramadan is not within the current analysis window.",
            }

        effects = {}
        years_in_data = set(r["year"] for r in series)

        for year, start in RAMADAN_STARTS.items():
            if year not in years_in_data:
                continue
            end = start + timedelta(days=RAMADAN_DURATION_DAYS)

            from apps.transactions.models import MaterialMovement
            ramadan_rev = (
                MaterialMovement.objects
                .filter(
                    company=company, movement_type="ف بيع",
                    movement_date__gte=start, movement_date__lte=end,
                )
                .aggregate(total=Sum("total_out"))
            )
            daily_avg_ramadan = float(ramadan_rev["total"] or 0) / RAMADAN_DURATION_DAYS

            prior_start = start - timedelta(days=30)
            prior_rev   = (
                MaterialMovement.objects
                .filter(
                    company=company, movement_type="ف بيع",
                    movement_date__gte=prior_start, movement_date__lt=start,
                )
                .aggregate(total=Sum("total_out"))
            )
            daily_avg_prior = float(prior_rev["total"] or 0) / 30

            if daily_avg_prior > 0:
                ramadan_index = daily_avg_ramadan / daily_avg_prior
                effects[year] = {
                    "year":           year,
                    "start":          str(start),
                    "end":            str(end),
                    "months":         [start.month, end.month],
                    "ramadan_index":  round(ramadan_index, 3),
                    "effect":         (
                        "boost"   if ramadan_index > 1.05 else
                        "drop"    if ramadan_index < 0.95 else
                        "neutral"
                    ),
                    "daily_avg_lyd":  round(daily_avg_ramadan, 2),
                }

        avg_effect = (
            sum(e["ramadan_index"] for e in effects.values()) / len(effects)
            if effects else 1.0
        )
        return {
            "detected":          bool(effects),
            "relevant":          True,
            "status":            relevant_ramadan["status"],
            "ramadan_window":    relevant_ramadan,
            "years_analyzed":    list(effects.keys()),
            "avg_ramadan_index": round(avg_effect, 3),
            "dominant_effect":   (
                "sales boost during Ramadan" if avg_effect > 1.05 else
                "sales slowdown during Ramadan" if avg_effect < 0.95 else
                "minimal Ramadan effect"
            ),
            "annual_effects": effects,
            "adjustment_note": (
                f"Ramadan effect: avg daily sales "
                f"{'increase' if avg_effect >= 1.0 else 'decrease'} "
                f"{abs(avg_effect - 1) * 100:.0f}% during Ramadan. "
                f"{'Act now — ' + relevant_ramadan['label']}" if relevant_ramadan else ""
            ),
        }

    # ── Trend & helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _compute_trend(series: list) -> dict:
        n = len(series)
        if n < 3:
            return {
                "direction": "insufficient_data",
                "slope_pct_per_month": 0.0, "r_squared": 0.0,
            }
        x  = list(range(n))
        y  = [row["revenue"] for row in series]
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        den = sum((xi - mx) ** 2 for xi in x)
        if den == 0:
            return {"direction": "flat", "slope_pct_per_month": 0.0, "r_squared": 0.0}
        slope     = num / den
        intercept = my - slope * mx
        y_hat     = [slope * xi + intercept for xi in x]
        ss_res    = sum((yi - yhi) ** 2 for yi, yhi in zip(y, y_hat))
        ss_tot    = sum((yi - my) ** 2 for yi in y)
        r2        = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
        slope_pct = (slope / my * 100) if my > 0 else 0.0
        direction = "growing" if slope_pct > 1 else "declining" if slope_pct < -1 else "stable"
        return {
            "direction": direction,
            "slope_pct_per_month": round(slope_pct, 3),
            "slope_lyd_per_month": round(slope, 2),
            "r_squared": round(r2, 4),
        }

    @staticmethod
    def _classify_months(indices: dict):
        peaks   = [m for m, v in indices.items() if v["label"] == "peak"]
        troughs = [m for m, v in indices.items() if v["label"] == "trough"]
        return peaks, troughs

    # ── Upcoming peak check (future only) ────────────────────────────────────

    @staticmethod
    def _check_upcoming_peak(peaks: list, today: date) -> bool:
        """
        Returns True only if a peak month is within the next 60 days
        (i.e. actually upcoming, never past).
        """
        for month_num in peaks:
            # Build target date for this year (or next if already passed)
            try:
                target = date(today.year, month_num, 1)
            except ValueError:
                continue
            if target < today:
                target = date(today.year + 1, month_num, 1)
            days_until = (target - today).days
            if 0 < days_until <= 60:
                return True
        return False

    # ── Current season label (only current or upcoming) ──────────────────────

    @staticmethod
    def _current_season_label(indices: dict, today: date) -> str:
        """
        Returns a label for the current month only if it's a peak or trough.
        Upcoming peak/trough within 60 days also flagged.
        """
        m    = today.month
        info = indices.get(m, {})
        si   = info.get("seasonality_index", 1.0)
        name = info.get("month_name", "")
        lbl  = info.get("label", "normal")

        if lbl == "peak":
            return f"Peak season — {name} (SI={si:.2f})"
        if lbl == "trough":
            return f"Low season — {name} (SI={si:.2f})"

        # Check next 2 months for an upcoming peak/trough
        for offset in [1, 2]:
            nm     = (m + offset - 1) % 12 + 1
            ninfo  = indices.get(nm, {})
            nlbl   = ninfo.get("label", "normal")
            nname  = ninfo.get("month_name", "")
            nsi    = ninfo.get("seasonality_index", 1.0)
            if nlbl == "peak":
                return f"Normal — {name} (peak approaches: {nname}, SI={nsi:.2f})"
            if nlbl == "trough":
                return f"Normal — {name} (trough approaches: {nname}, SI={nsi:.2f})"

        return f"Normal demand — {name} (SI={si:.2f})" if si else f"Normal demand — {name}"

    # ── Category patterns ─────────────────────────────────────────────────────

    def _compute_category_patterns(self, company) -> list:
        try:
            from apps.transactions.models import MaterialMovement
            today      = date.today()
            start_date = today.replace(day=1) - timedelta(days=HISTORY_MONTHS * 31)
            rows = (
                MaterialMovement.objects
                .filter(company=company, movement_type="ف بيع", movement_date__gte=start_date)
                .annotate(month=TruncMonth("movement_date"))
                .values("month", "category")
                .annotate(revenue=Sum("total_out"))
                .order_by("category", "month")
            )
            by_cat: dict = defaultdict(lambda: defaultdict(list))
            for row in rows:
                cat = row.get("category") or "Unclassified"
                by_cat[cat][row["month"].month].append(float(row["revenue"] or 0))
            results = []
            for cat, monthly_data in list(by_cat.items())[:8]:
                all_vals = [v for vs in monthly_data.values() for v in vs]
                if not all_vals:
                    continue
                overall_avg = sum(all_vals) / len(all_vals)
                month_si = {
                    m: sum(vs) / len(vs) / overall_avg
                    for m, vs in monthly_data.items()
                    if vs and overall_avg > 0
                }
                if not month_si:
                    continue
                peak_m   = max(month_si, key=month_si.get)
                trough_m = min(month_si, key=month_si.get)
                results.append({
                    "category":         cat,
                    "peak_month":       peak_m,
                    "peak_month_name":  MONTH_NAMES[peak_m],
                    "peak_index":       round(month_si[peak_m], 4),
                    "trough_month":     trough_m,
                    "trough_month_name":MONTH_NAMES[trough_m],
                    "trough_index":     round(month_si[trough_m], 4),
                })
            return results
        except Exception:
            return []

    # ── AI call ───────────────────────────────────────────────────────────────

    def _call_ai(
        self, indices, peaks, troughs, trend,
        ramadan_analysis, relevant_ramadan,
        exchange_rate, company_id,
    ) -> dict | None:
        index_lines = []
        for m, v in sorted(indices.items()):
            si = v["seasonality_index"]
            if si:
                bar = "█" * min(20, int(si * 10))
                index_lines.append(
                    f"  {v['month_name']:<12} SI={si:.4f}  {bar}  [{v['label'].upper()}]"
                )

        # Ramadan section (only when relevant)
        ramadan_note = ""
        if relevant_ramadan and ramadan_analysis.get("detected"):
            ramadan_note = (
                f"\nRAMADAN ALERT — {relevant_ramadan['label']}\n"
                f"Historical effect: {ramadan_analysis['dominant_effect']} "
                f"(avg index={ramadan_analysis['avg_ramadan_index']:.3f})"
            )
        elif not relevant_ramadan:
            ramadan_note = "\nRamadan: not within current analysis window — exclude from recommendations."

        # Exchange rate context
        rate = exchange_rate["usd_to_lyd"]
        fx_note = (
            f"\nExchange rate: 1 USD = {rate:.2f} LYD "
            f"(1 LYD = {exchange_rate['lyd_to_usd']:.4f} USD) — "
            f"source: {exchange_rate['source']}"
        )

        user_prompt = (
            f"Seasonality Analysis — Libyan B2B Distribution\n"
            f"History window: {HISTORY_MONTHS} months (current year)\n"
            f"Trend: {trend['direction']} "
            f"({trend.get('slope_pct_per_month', 0):+.2f}%/mo, "
            f"R²={trend.get('r_squared', 0):.3f})"
            f"{fx_note}"
            f"{ramadan_note}\n\n"
            f"Monthly Seasonality Indices:\n" + "\n".join(index_lines) + "\n\n"
            f"Peak months: {', '.join(MONTH_NAMES[m] for m in peaks) or 'None'}\n"
            f"Trough months: {', '.join(MONTH_NAMES[m] for m in troughs) or 'None'}\n\n"
            f"Generate recommendations ONLY for current or upcoming periods, "
            f"not for months that have already passed this year."
        )
        return self._client.complete(
            system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            model="smart", max_tokens=800,
            analyzer="seasonal_analyzer", company_id=str(company_id),
        )

    # ── Format result ─────────────────────────────────────────────────────────

    def _format_result(
        self, indices, trend, peaks, troughs, category_pats,
        upcoming_alert, current_season, ramadan_analysis,
        relevant_ramadan, exchange_rate, ai_result, today: date,
    ) -> dict:
        narrative  = self._default_narrative(indices, peaks, troughs, trend)
        recs       = self._default_recommendations(peaks, troughs, today)
        stock_cal  = self._default_stock_calendar(peaks, today)
        staffing   = self._default_staffing(indices, peaks)
        confidence = "medium"

        if ai_result and not ai_result.get("error"):
            narrative   = ai_result.get("seasonal_narrative",         narrative)
            recs        = ai_result.get("ai_recommendations",         recs)
            stock_cal   = ai_result.get("stock_preparation_calendar", stock_cal)
            staffing    = ai_result.get("staffing_implications",      staffing)
            confidence  = ai_result.get("confidence",                 "medium")

        return {
            "history_months":   HISTORY_MONTHS,
            "analysis_year":    today.year,
            "current_season":   current_season,
            "upcoming_peak_alert": upcoming_alert,
            "trend":            trend,
            "exchange_rate":    exchange_rate,          # ← New field
            "seasonality_indices": indices,
            "peak_months":      peaks,
            "peak_month_names": [MONTH_NAMES[m] for m in peaks],
            "trough_months":    troughs,
            "trough_month_names": [MONTH_NAMES[m] for m in troughs],
            "category_patterns": category_pats,
            "ramadan_analysis": ramadan_analysis,       # Only relevant Ramadan info
            "relevant_ramadan": relevant_ramadan,       # ← New: None when not relevant
            "seasonal_narrative": narrative,
            "stock_preparation_calendar": stock_cal,
            "staffing_implications": staffing,
            "ai_recommendations": recs,
            "confidence": confidence,
        }

    # ── Default fallbacks ─────────────────────────────────────────────────────

    @staticmethod
    def _default_narrative(indices, peaks, troughs, trend) -> str:
        peak_names   = [MONTH_NAMES[m] for m in peaks]
        trough_names = [MONTH_NAMES[m] for m in troughs]
        parts = []
        if peak_names:
            valid_si = [
                indices[m]["seasonality_index"] for m in peaks
                if indices[m]["seasonality_index"]
            ]
            if valid_si:
                avg_si = sum(valid_si) / len(valid_si)
                parts.append(
                    f"Peak demand in {', '.join(peak_names)} "
                    f"(avg SI={avg_si:.2f} — {int((avg_si - 1) * 100)}% above average)."
                )
        if trough_names:
            parts.append(f"Weakest demand in {', '.join(trough_names)}.")
        parts.append(
            f"Overall trend: {trend['direction']} "
            f"at {trend.get('slope_pct_per_month', 0):+.2f}%/month."
        )
        return " ".join(parts)

    @staticmethod
    def _default_recommendations(peaks: list, troughs: list, today: date) -> list:
        """
        Only recommend for months that are UPCOMING (not yet passed this year).
        """
        recs = []
        for month_num in peaks:
            try:
                target = date(today.year, month_num, 1)
            except ValueError:
                continue
            if target < today:
                target = date(today.year + 1, month_num, 1)
            # Only show if within the next 6 months
            days_until = (target - today).days
            if days_until <= 180:
                recs.append(
                    f"Begin inventory build-up 6 weeks before {MONTH_NAMES[month_num]} "
                    f"to avoid stock-outs ({days_until} days from now)."
                )
        for month_num in troughs:
            try:
                target = date(today.year, month_num, 1)
            except ValueError:
                continue
            if target < today:
                target = date(today.year + 1, month_num, 1)
            days_until = (target - today).days
            if days_until <= 180:
                recs.append(
                    f"Use {MONTH_NAMES[month_num]} for supplier negotiations "
                    f"and warehouse reorganization ({days_until} days from now)."
                )
        recs.append(
            "Review seasonality indices quarterly to detect demand pattern shifts."
        )
        return recs[:4]

    @staticmethod
    def _default_stock_calendar(peaks: list, today: date) -> list:
        """
        Build stock preparation entries only for upcoming peak months.
        Past peaks are excluded entirely.
        """
        calendar = []
        for m in peaks:
            try:
                peak_date = date(today.year, m, 1)
            except ValueError:
                continue
            if peak_date < today:
                peak_date = date(today.year + 1, m, 1)
            # The prep month is 6 weeks (≈1.5 months) before the peak
            prep_month_num = ((peak_date.month - 2) % 12) + 1
            prep_year      = peak_date.year if peak_date.month > 1 else peak_date.year - 1
            days_to_peak   = (peak_date - today).days
            if days_to_peak > 180:
                continue  # Too far in the future — skip
            calendar.append({
                "month":            MONTH_NAMES.get(prep_month_num, ""),
                "prep_year":        prep_year,
                "action":           f"Place large orders ahead of {MONTH_NAMES[m]} peak",
                "lead_time_weeks":  6,
                "rationale":        (
                    f"SI ≥ {PEAK_THRESHOLD} — build buffer stock 6 weeks in advance. "
                    f"Peak in {days_to_peak} days."
                ),
                "days_to_peak":     days_to_peak,
            })
        return calendar

    @staticmethod
    def _default_staffing(indices, peaks) -> str:
        if not peaks:
            return "No significant seasonal staffing implications detected."
        valid_si = [indices[m]["seasonality_index"] for m in peaks if indices[m]["seasonality_index"]]
        if not valid_si:
            return "Insufficient data for staffing recommendations."
        max_si = max(valid_si)
        pct    = int((max_si - 1) * 100)
        return (
            f"During {', '.join(MONTH_NAMES[m] for m in peaks)}, "
            f"expect up to {pct}% more order volume. "
            f"Plan for additional delivery staff and extended warehouse hours."
        )

    @staticmethod
    def _empty_result(reason: str) -> dict:
        return {
            "error": reason,
            "seasonality_indices": {}, "peak_months": [],
            "trough_months": [], "trend": {}, "category_patterns": [],
            "ramadan_analysis": {}, "relevant_ramadan": None,
            "exchange_rate": _get_exchange_rate(),
            "ai_recommendations": [], "confidence": "low",
        }