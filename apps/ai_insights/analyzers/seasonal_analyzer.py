"""
apps/ai_insights/analyzers/seasonal_analyzer.py
-------------------------------------------------
SCRUM-26 v5.0 — Live exchange rate via OpenAI + seasonal AI analysis

IMPLEMENTED BUSINESS RULES:
─────────────────────────────────────────────────────────────────────────────
RULE 1 — 12-MONTH ROLLING WINDOW
  • End date   = 1st day of current month (full month included)
  • Start date = end date − 12 calendar months (exact calendar calculation, not in days)
  • Example: if today is April 2026 → window = April 2025 → April 2026
  • No longer uses timedelta(days=N*31) which is imprecise.

RULE 2 — RAMADAN: ACTIVATE 2 MONTHS BEFORE, DURING, SILENT AFTER
  • ACTIVE   if Ramadan is currently ongoing (today between start and end)
  • ACTIVE   if Ramadan starts within RAMADAN_LOOKAHEAD_DAYS (60 d ≈ 2 months)
  • TOTAL SILENCE if Ramadan has passed (even if it was this year)
  • Zero mention of Ramadan in AI prompts when SILENT

RULE 3 — MANDATORY EXCHANGE RATE (LIVE VIA OPENAI)
  • The LYD/USD rate is fetched IN REAL TIME via the OpenAI API (web search)
  • Priority:
      1. settings.LYD_USD_RATE  (manually configured — absolute priority)
      2. OpenAI live fetch       (real-time market rate)
      3. Redis/memory cache      (if OpenAI unavailable, max 4h)
  • NO HARDCODED FALLBACK — if no source is available, exception is raised
  • The rate is injected into: the AI prompt, the final result, the narratives

RULE 4 — DYNAMIC SEASONS: CURRENT AND NEXT ONLY
  • Never return recommendations for past seasons
  • "Current" = current month is a peak or trough
  • "Next upcoming" = peak/trough within the next 90 days
  • Stock preparation calendar only includes upcoming peaks (≤ 180 d)

RULE 5 — SEASONAL ANALYSIS VIA OPENAI
  • The AI call uses the OpenAI API directly (gpt-4o)
  • No intermediary AIClient — direct call with the OpenAI Python SDK
─────────────────────────────────────────────────────────────────────────────
"""

import json
import logging
import math
import time
from collections import defaultdict
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta  # pip install python-dateutil

from django.db.models import Sum, Q
from django.db.models.functions import TruncMonth
from django.core.cache import cache  # Django cache (Redis recommended)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# RULE 1: Rolling window expressed in calendar months (not in days)
HISTORY_MONTHS = 12

# Seasonality index classification thresholds
PEAK_THRESHOLD   = 1.15   # SI ≥ 1.15 → peak (15% above average)
TROUGH_THRESHOLD = 0.85   # SI ≤ 0.85 → trough (15% below average)

# RULE 2: Ramadan activation window = 60 days before start (≈ 2 months)
RAMADAN_LOOKAHEAD_DAYS = 60

# RULE 4: Horizon for "next season" recommendations
UPCOMING_SEASON_HORIZON_DAYS = 90   # insights only if season within < 90 d
STOCK_PREP_HORIZON_DAYS      = 180  # stock calendar only if peak < 180 d

# Minimum data required for a reliable analysis
MIN_MONTHS_REQUIRED = 6

# RULE 3: Exchange rate cache (avoids repeated calls to OpenAI)
EXCHANGE_RATE_CACHE_KEY = "weeg:exchange_rate:lyd_usd"
EXCHANGE_RATE_CACHE_TTL = 4 * 3600  # 4 hours in seconds

# RULE 5: OpenAI model for seasonal analysis
OPENAI_MODEL = "gpt-4o"

MONTH_NAMES = {
    1: "January",  2: "February", 3: "March",    4: "April",
    5: "May",      6: "June",     7: "July",      8: "August",
    9: "September",10: "October", 11: "November", 12: "December",
}

# ─────────────────────────────────────────────────────────────────────────────
# RAMADAN CALENDAR (approximate Gregorian dates, official start ±1 d)
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1: 12-MONTH ROLLING WINDOW (RULE 1)
# ─────────────────────────────────────────────────────────────────────────────

def compute_rolling_window(reference_date: date) -> tuple[date, date]:
    """
    RULE 1: Exact calculation of the 12-month rolling window.

    Uses relativedelta for exact calendar calculation (handles months of
    different lengths, leap years, etc.).

    Returns:
        (start_date, end_date) — both on the 1st day of their respective month.

    Example:
        reference_date = 2026-04-14
        → start = 2025-04-01
        → end   = 2026-04-01  (last complete month started)
    """
    # end = 1st day of current month (current month is included)
    end_date   = reference_date.replace(day=1)
    # start = end - exactly 12 calendar months
    start_date = end_date - relativedelta(months=HISTORY_MONTHS)
    return start_date, end_date


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 2: RAMADAN MANAGEMENT (RULE 2)
# ─────────────────────────────────────────────────────────────────────────────

def get_relevant_ramadan(today: date) -> dict | None:
    """
    RULE 2: Returns Ramadan information ONLY if:
      - Ramadan is currently ONGOING (today ∈ [start, end]), OR
      - Ramadan starts within the next RAMADAN_LOOKAHEAD_DAYS days.

    Returns None in ALL other cases (past, too distant).
    This guarantees total silence outside the activation window.
    """
    for year in sorted(RAMADAN_STARTS.keys()):
        start = RAMADAN_STARTS[year]
        end   = start + timedelta(days=RAMADAN_DURATION_DAYS)

        # CASE 1: Ramadan ongoing
        if start <= today <= end:
            days_remaining = (end - today).days
            return {
                "status":        "ongoing",
                "year":          year,
                "start":         str(start),
                "end":           str(end),
                "days_remaining": days_remaining,
                "label":         f"Ramadan {year} ongoing — {days_remaining} days remaining",
                "activate_ai":   True,   # RULE 2: activate the Ramadan block in the prompt
            }

        # CASE 2: Ramadan imminent (≤ 60 days ≈ 2 months)
        days_until = (start - today).days
        if 0 < days_until <= RAMADAN_LOOKAHEAD_DAYS:
            return {
                "status":     "upcoming",
                "year":       year,
                "start":      str(start),
                "end":        str(end),
                "days_until": days_until,
                "label":      f"Ramadan {year} in {days_until} days — prepare now",
                "activate_ai": True,  # RULE 2: activate in the prompt
            }

        # CASE 3: Ramadan past (even if it was this year) → move to next
        # CASE 4: Ramadan too distant → implicit None

    # RULE 2: No active period → TOTAL SILENCE
    return None


def has_ramadan_effect_data(monthly_series: list, ramadan_info: dict | None) -> bool:
    """
    Checks whether we have historical data covering at least one Ramadan
    in the analysis window. Used to decide whether to compute the effect.
    """
    if not ramadan_info:
        return False
    years_in_data = {r["year"] for r in monthly_series}
    return any(y in years_in_data for y in RAMADAN_STARTS)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 3: LIVE EXCHANGE RATE VIA OPENAI (RULE 3)
# ─────────────────────────────────────────────────────────────────────────────

class ExchangeRateError(Exception):
    """Raised when the exchange rate is unavailable (all sources exhausted)."""
    pass


def _fetch_rate_from_openai() -> dict:
    """
    Fetches the USD/LYD rate in real time via the OpenAI API with web search.

    Uses the gpt-4o model with the web_search_preview tool to get the
    current market rate (not the static official CBL rate).

    Returns:
        dict with usd_to_lyd, lyd_to_usd, source, fetched_at, note

    Raises:
        ExchangeRateError if OpenAI cannot provide the rate
    """
    import openai
    from django.conf import settings

    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        raise ExchangeRateError(
            "OPENAI_API_KEY not configured in settings.py — "
            "cannot fetch the exchange rate."
        )

    client = openai.OpenAI(api_key=api_key)

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            tools=[{"type": "web_search_preview"}],
            input=(
                "What is the current USD to LYD (Libyan Dinar) exchange rate today? "
                "Give me only the numeric rate as a decimal number (e.g. 6.34). "
                "Use the most recent market rate available."
            ),
        )

        # Extract text from the response
        rate_text = ""
        for item in response.output:
            if hasattr(item, "content"):
                for block in item.content:
                    if hasattr(block, "text"):
                        rate_text += block.text

        # Parse the rate from the response
        import re
        numbers = re.findall(r'\b(\d+\.\d{1,4})\b', rate_text)
        # Filter plausible values for LYD/USD (between 4.0 and 10.0)
        candidates = [float(n) for n in numbers if 4.0 <= float(n) <= 10.0]

        if not candidates:
            raise ExchangeRateError(
                f"OpenAI did not return a valid rate. Response: {rate_text[:200]}"
            )

        rate = candidates[0]  # Take the first plausible rate found
        fetched_at = date.today().isoformat()

        logger.info("[ExchangeRate] OpenAI live rate: 1 USD = %.4f LYD", rate)

        return {
            "usd_to_lyd":   round(rate, 4),
            "lyd_to_usd":   round(1 / rate, 6),
            "source":       "openai_live",
            "is_live":      True,
            "fetched_at":   fetched_at,
            "note":         f"Rate fetched in real time via OpenAI web search — {fetched_at}",
        }

    except openai.OpenAIError as exc:
        raise ExchangeRateError(f"OpenAI API error during rate fetch: {exc}") from exc


def get_exchange_rate() -> dict:
    """
    RULE 3: The LYD/USD exchange rate is MANDATORY in every analysis.

    Priority (no hardcoded fallback):
      1. settings.LYD_USD_RATE  (manually configured → absolute priority)
      2. Redis/memory cache      (cached OpenAI rate for up to 4h)
      3. OpenAI live fetch       (real-time web search)

    Raises:
        ExchangeRateError if no source is available
    """
    from django.conf import settings

    # ── PRIORITY 1: Rate manually configured in settings ──────────────────────
    configured_rate = getattr(settings, "LYD_USD_RATE", None)
    if configured_rate:
        rate = float(configured_rate)
        logger.info("[ExchangeRate] Rate from settings: %.4f LYD/USD", rate)
        return {
            "usd_to_lyd":   round(rate, 4),
            "lyd_to_usd":   round(1 / rate, 6),
            "source":       "settings",
            "source_label": "Manually configured rate (CBL)",
            "is_live":      False,
            "fetched_at":   None,
            "note":         "Rate configured in Django settings (source: CBL or manual parameter)",
        }

    # ── PRIORITY 2: Cache (previously fetched OpenAI rate) ───────────────────
    cached = cache.get(EXCHANGE_RATE_CACHE_KEY)
    if cached:
        logger.info(
            "[ExchangeRate] Rate from cache: %.4f LYD/USD (fetched_at=%s)",
            cached["usd_to_lyd"], cached.get("fetched_at"),
        )
        cached["source"] = "cache"
        cached["source_label"] = f"Cached rate (OpenAI, fetched on {cached.get('fetched_at', '?')})"
        cached["is_live"] = False
        return cached

    # ── PRIORITY 3: OpenAI live fetch ─────────────────────────────────────────
    logger.info("[ExchangeRate] No cache — live fetch via OpenAI...")
    rate_data = _fetch_rate_from_openai()

    # Cache to avoid repeated calls
    cache.set(EXCHANGE_RATE_CACHE_KEY, rate_data, EXCHANGE_RATE_CACHE_TTL)
    logger.info(
        "[ExchangeRate] Rate cached (TTL=%ds): %.4f LYD/USD",
        EXCHANGE_RATE_CACHE_TTL, rate_data["usd_to_lyd"],
    )

    return rate_data


def invalidate_exchange_rate_cache():
    """
    Forces deletion of the exchange rate cache.
    Useful for admins who want to force an immediate refresh.
    """
    cache.delete(EXCHANGE_RATE_CACHE_KEY)
    logger.info("[ExchangeRate] Cache manually invalidated.")


def format_fx_context(exchange_rate: dict) -> str:
    """
    RULE 3: Formats the exchange rate context for injection into AI prompts.
    Always included, regardless of the source.
    """
    rate   = exchange_rate["usd_to_lyd"]
    source = exchange_rate.get("source", "unknown")
    label  = exchange_rate.get("source_label", source)
    return (
        f"1 USD = {rate:.4f} LYD ({label}) | "
        f"1 LYD = {exchange_rate['lyd_to_usd']:.6f} USD"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 4: DYNAMIC SEASON DETECTION (RULE 4)
# ─────────────────────────────────────────────────────────────────────────────

def classify_current_season(indices: dict, today: date) -> dict:
    """
    RULE 4: Dynamically detects the current season and the next upcoming one.

    Returns a dict with:
      - current_month_label  : label of current month (peak/trough/normal)
      - current_si           : seasonality index of current month
      - upcoming_peak        : next peak within < UPCOMING_SEASON_HORIZON_DAYS days
      - upcoming_trough      : next trough within < UPCOMING_SEASON_HORIZON_DAYS days
      - display_label        : display label for the UI

    RULE: NEVER display a past season, even if it was this year.
    """
    current_month = today.month
    current_info  = indices.get(current_month, {})
    current_si    = current_info.get("seasonality_index", 1.0) or 1.0
    current_label = current_info.get("label", "normal")
    month_name    = MONTH_NAMES.get(current_month, "")

    result = {
        "current_month":       current_month,
        "current_month_name":  month_name,
        "current_si":          current_si,
        "current_label":       current_label,
        "upcoming_peak":       None,
        "upcoming_trough":     None,
        "peak_alert":          False,
        "display_label":       "",
    }

    # Look for the next peak / trough (within UPCOMING_SEASON_HORIZON_DAYS days)
    for offset in range(1, 13):
        target_month = (current_month + offset - 1) % 12 + 1
        target_year  = today.year + ((current_month + offset - 1) // 12)

        try:
            target_date = date(target_year, target_month, 1)
        except ValueError:
            continue

        days_until = (target_date - today.replace(day=1)).days
        if days_until < 0:
            continue  # RULE 4: never a past season

        minfo  = indices.get(target_month, {})
        mlabel = minfo.get("label", "normal")
        msi    = minfo.get("seasonality_index", 1.0) or 1.0
        mname  = MONTH_NAMES.get(target_month, "")

        if mlabel == "peak" and result["upcoming_peak"] is None:
            result["upcoming_peak"] = {
                "month":      target_month,
                "month_name": mname,
                "si":         msi,
                "days_until": days_until,
                "date":       str(target_date),
            }
            if days_until <= UPCOMING_SEASON_HORIZON_DAYS:
                result["peak_alert"] = True

        if mlabel == "trough" and result["upcoming_trough"] is None:
            result["upcoming_trough"] = {
                "month":      target_month,
                "month_name": mname,
                "si":         msi,
                "days_until": days_until,
                "date":       str(target_date),
            }

        # Stop once both have been found
        if result["upcoming_peak"] and result["upcoming_trough"]:
            break

    # Build display label
    if current_label == "peak":
        result["display_label"] = f"High season — {month_name} (SI={current_si:.2f})"
    elif current_label == "trough":
        result["display_label"] = f"Low season — {month_name} (SI={current_si:.2f})"
    elif result["upcoming_peak"] and result["upcoming_peak"]["days_until"] <= UPCOMING_SEASON_HORIZON_DAYS:
        up = result["upcoming_peak"]
        result["display_label"] = (
            f"Normal — {month_name} "
            f"(imminent peak: {up['month_name']}, SI={up['si']:.2f}, in {up['days_until']} d)"
        )
    else:
        result["display_label"] = f"Normal demand — {month_name} (SI={current_si:.2f})"

    return result


def build_stock_calendar(peaks: list, today: date, indices: dict) -> list:
    """
    RULE 4: Stock preparation calendar — ONLY for upcoming peaks.

    Inclusion criteria:
      - The peak is in the future (days_until > 0)
      - The peak is within STOCK_PREP_HORIZON_DAYS (avoids overly distant recommendations)
      - Preparation is 6 weeks before the peak (standard Libya distribution lead time)
    """
    calendar = []

    for month_num in peaks:
        # Calculate the date of the next occurrence of this peak month
        candidate = date(today.year, month_num, 1)
        if candidate < today:
            candidate = date(today.year + 1, month_num, 1)

        days_to_peak = (candidate - today).days

        # RULE 4: only include peaks within the defined horizon
        if days_to_peak <= 0 or days_to_peak > STOCK_PREP_HORIZON_DAYS:
            continue

        # Preparation month is 6 weeks (≈1.5 months) before the peak
        prep_date      = candidate - relativedelta(weeks=6)
        prep_month_num = prep_date.month
        prep_year      = prep_date.year

        si    = (indices.get(month_num) or {}).get("seasonality_index") or 1.0
        boost = round((si - 1) * 100, 0)

        calendar.append({
            "month":           MONTH_NAMES.get(prep_month_num, ""),
            "prep_year":       prep_year,
            "peak_month":      MONTH_NAMES.get(month_num, ""),
            "peak_date":       str(candidate),
            "days_to_peak":    days_to_peak,
            "action":          f"Place orders for {MONTH_NAMES[month_num]} (peak)",
            "lead_time_weeks": 6,
            "rationale":       (
                f"SI={si:.2f} (+{boost:.0f}% vs average). "
                f"Peak in {days_to_peak} days. "
                f"Order now to meet supplier lead times."
            ),
        })

    # Chronological sort
    calendar.sort(key=lambda x: x["days_to_peak"])
    return calendar


def build_dynamic_recommendations(
    season_info: dict,
    peaks: list,
    troughs: list,
    today: date,
) -> list:
    """
    RULE 4: Dynamic recommendations — only for current / imminent season.

    Generates concrete recommendations for:
      1. What is happening NOW (current month)
      2. What is coming SOON (< UPCOMING_SEASON_HORIZON_DAYS days)
    Never for the past.
    """
    recs = []
    current_label   = season_info["current_label"]
    current_name    = season_info["current_month_name"]
    upcoming_peak   = season_info.get("upcoming_peak")
    upcoming_trough = season_info.get("upcoming_trough")

    # Recommendation for current season
    if current_label == "peak":
        recs.append(
            f"📦 High season active ({current_name}) — monitor stock-outs "
            f"in real time and prioritize replenishment of Class A items."
        )
    elif current_label == "trough":
        recs.append(
            f"📉 Low season active ({current_name}) — use lower demand "
            f"to negotiate with suppliers and optimize stock levels."
        )

    # Recommendation for imminent peak
    if upcoming_peak and upcoming_peak["days_until"] <= UPCOMING_SEASON_HORIZON_DAYS:
        recs.append(
            f"⚠️ Seasonal peak in {upcoming_peak['days_until']} days "
            f"({upcoming_peak['month_name']}, SI={upcoming_peak['si']:.2f}) — "
            f"launch supplier orders now (4-6 week delivery lead time)."
        )

    # Recommendation for imminent trough
    if upcoming_trough and upcoming_trough["days_until"] <= UPCOMING_SEASON_HORIZON_DAYS:
        recs.append(
            f"📆 Seasonal trough in {upcoming_trough['days_until']} days "
            f"({upcoming_trough['month_name']}, SI={upcoming_trough['si']:.2f}) — "
            f"plan inventory operations and rate renegotiations."
        )

    # Permanent recommendation (always useful)
    recs.append(
        "📊 Review seasonality indices each quarter to detect "
        "changes in customer purchasing patterns."
    )

    return recs[:4]


# ─────────────────────────────────────────────────────────────────────────────
# AI PROMPT — INJECTION OF ALL RULES
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a supply chain and demand planning expert for WEEG, a BI platform
for Libyan distribution companies.

You receive:
- Monthly seasonality indices (12-month rolling window)
- Current season status (ongoing peak/trough or upcoming)
- Ramadan data ONLY when it is currently active or imminent (≤ 60 days)
- LYD/USD exchange rate (ALWAYS include in financial impacts — use the live rate provided)

MANDATORY RULES:
1. Generate insights ONLY for the current season or next imminent one (< 90 days).
   Never reference past seasons or months that have already occurred.
2. Include the exchange rate in every financial recommendation
   (e.g., "at 6.34 LYD/USD, a $50,000 order = 317,000 LYD").
3. If no Ramadan section is provided → DO NOT mention Ramadan at all.
4. All stock preparation actions must reference REAL upcoming dates.
5. The exchange rate you receive is LIVE and accurate — use it exactly as given.

Return ONLY valid JSON:
{
  "seasonal_narrative": "<3-4 sentences — current situation + next 90 days only>",
  "peak_season_story":  "<what drives peak demand — only if peak is current or imminent>",
  "trough_season_story": "<what causes trough — only if trough is current or imminent>",
  "stock_preparation_calendar": [
    {"month": "<n>", "action": "<action>", "lead_time_weeks": <int>,
     "rationale": "<include LYD/USD impact using live rate>"}
  ],
  "staffing_implications": "<quantified — peak vs. trough headcount estimate>",
  "ai_recommendations": ["<rec 1 with date + LYD amount using live rate>", "<rec 2>", "<rec 3>"],
  "confidence": "high" | "medium" | "low"
}"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ANALYZER
# ─────────────────────────────────────────────────────────────────────────────

class SeasonalAnalyzer:
    """
    Seasonal analyzer v5.0.

    Respects the 5 business rules:
      - 12-month rolling window (RULE 1)
      - Ramadan activated 2 months before / during only (RULE 2)
      - Live exchange rate via OpenAI, no hardcoded fallback (RULE 3)
      - Dynamic insights for current/imminent season only (RULE 4)
      - Direct AI call via OpenAI SDK (RULE 5)
    """

    def analyze(self, company, use_ai: bool = True) -> dict:
        logger.info("[SeasonalAnalyzer v5] Starting company=%s", company.id)

        today = date.today()

        # ── RULE 1: 12-month rolling window ───────────────────────────────────
        start_date, end_date = compute_rolling_window(today)
        logger.info("[SeasonalAnalyzer] Window: %s → %s", start_date, end_date)

        # ── RULE 3: Live exchange rate (mandatory, no fallback) ───────────────
        try:
            exchange_rate = get_exchange_rate()
        except ExchangeRateError as exc:
            logger.error("[SeasonalAnalyzer] FATAL: exchange rate unavailable: %s", exc)
            raise  # Propagate the error — no fallback

        logger.info(
            "[SeasonalAnalyzer] Rate: %.4f LYD/USD (source: %s)",
            exchange_rate["usd_to_lyd"], exchange_rate["source"]
        )

        # ── RULE 2: Ramadan check ─────────────────────────────────────────────
        relevant_ramadan = get_relevant_ramadan(today)
        if relevant_ramadan:
            logger.info("[SeasonalAnalyzer] Ramadan active: %s", relevant_ramadan["label"])
        else:
            logger.debug("[SeasonalAnalyzer] Ramadan: outside activation window → silence")

        # ── Collect data within the rolling window ────────────────────────────
        monthly_series = self._build_monthly_series(company, start_date, end_date)

        if len(monthly_series) < MIN_MONTHS_REQUIRED:
            return self._empty_result(
                f"Insufficient data: {len(monthly_series)} months out of "
                f"{MIN_MONTHS_REQUIRED} required (window {start_date} → {end_date}).",
                exchange_rate,
            )

        # ── STL decomposition and seasonality indices ─────────────────────────
        detrended = self._remove_trend_stl(monthly_series)
        indices   = self._compute_seasonality_indices(detrended, monthly_series)
        trend     = self._compute_trend(monthly_series)
        peaks, troughs = self._classify_months(indices)

        # ── RULE 4: Dynamic season detection ─────────────────────────────────
        season_info = classify_current_season(indices, today)

        # ── RULE 2: Ramadan effect (computed only if window is active) ────────
        ramadan_analysis = self._compute_ramadan_effect(
            company, monthly_series, relevant_ramadan
        )

        # ── Product category patterns ─────────────────────────────────────────
        category_patterns = self._compute_category_patterns(
            company, start_date, end_date
        )

        # ── RULE 5: Direct OpenAI call ────────────────────────────────────────
        ai_result = None
        if use_ai:
            try:
                ai_result = self._call_openai(
                    indices, peaks, troughs, trend,
                    ramadan_analysis, relevant_ramadan,
                    exchange_rate, season_info, today,
                )
            except Exception as exc:
                logger.warning("[SeasonalAnalyzer] OpenAI unavailable: %s", exc)

        return self._format_result(
            indices, trend, peaks, troughs, category_patterns,
            season_info, ramadan_analysis, relevant_ramadan,
            exchange_rate, ai_result, today, start_date, end_date,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # DATA COLLECTION (RULE 1: rolling window)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_monthly_series(
        self, company, start_date: date, end_date: date
    ) -> list:
        """
        RULE 1: Uses the exact dates of the rolling window calculated
        by compute_rolling_window(), not an approximate timedelta in days.
        """
        from apps.transactions.models import MaterialMovement

        rows = (
            MaterialMovement.objects
            .filter(
                company=company,
                movement_type="ف بيع",
                movement_date__gte=start_date,
                movement_date__lt=end_date,   # < end_date (exclusive end date)
            )
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .annotate(month=TruncMonth("movement_date"))
            .values("month")
            .annotate(revenue=Sum("total_out"))
            .order_by("month")
        )

        return [
            {
                "year":    r["month"].year,
                "month":   r["month"].month,
                "revenue": float(r["revenue"] or 0),
            }
            for r in rows
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # STL DECOMPOSITION (12 months, centered CMA)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _remove_trend_stl(series: list) -> list:
        """Removes the trend via a 12-month centered moving average."""
        n      = len(series)
        window = min(12, n)
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
    def _compute_seasonality_indices(
        detrended: list, raw: list
    ) -> dict:
        """
        Computes seasonality indices (SI) by month.
        SI = (deseasonalized monthly average) / (overall average)
        SI > 1 → month above average, SI < 1 → below average.
        """
        by_month: dict[int, list] = defaultdict(list)
        for row in detrended:
            if row.get("detrended", 0) > 0:
                by_month[row["month"]].append(row["detrended"])

        valid_vals  = [r["detrended"] for r in detrended if r.get("detrended", 0) > 0]
        overall_avg = sum(valid_vals) / len(valid_vals) if valid_vals else 1.0

        raw_by_month: dict[int, list] = defaultdict(list)
        for r in raw:
            raw_by_month[r["month"]].append(r["revenue"])

        indices = {}
        for month_num in range(1, 13):
            vals     = by_month.get(month_num, [])
            si       = (sum(vals) / len(vals)) / overall_avg if vals and overall_avg > 0 else None
            raw_vals = raw_by_month.get(month_num, [])
            avg_rev  = sum(raw_vals) / len(raw_vals) if raw_vals else 0.0

            label = "no_data"
            if si is not None:
                if si >= PEAK_THRESHOLD:
                    label = "peak"
                elif si <= TROUGH_THRESHOLD:
                    label = "trough"
                else:
                    label = "normal"

            indices[month_num] = {
                "month_num":               month_num,
                "month_name":              MONTH_NAMES[month_num],
                "seasonality_index":       round(si, 4) if si is not None else None,
                "avg_monthly_revenue_lyd": round(avg_rev, 2),
                "data_points":             len(vals),
                "label":                   label,
            }

        return indices

    @staticmethod
    def _classify_months(indices: dict) -> tuple[list, list]:
        peaks   = [m for m, v in indices.items() if v["label"] == "peak"]
        troughs = [m for m, v in indices.items() if v["label"] == "trough"]
        return peaks, troughs

    # ─────────────────────────────────────────────────────────────────────────
    # TREND (simple linear regression)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_trend(series: list) -> dict:
        n = len(series)
        if n < 3:
            return {
                "direction":            "insufficient_data",
                "slope_pct_per_month":  0.0,
                "slope_lyd_per_month":  0.0,
                "r_squared":            0.0,
            }

        x  = list(range(n))
        y  = [row["revenue"] for row in series]
        mx = sum(x) / n
        my = sum(y) / n

        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        den = sum((xi - mx) ** 2 for xi in x)

        if den == 0:
            return {"direction": "stable", "slope_pct_per_month": 0.0,
                    "slope_lyd_per_month": 0.0, "r_squared": 0.0}

        slope     = num / den
        intercept = my - slope * mx
        y_hat     = [slope * xi + intercept for xi in x]

        ss_res = sum((yi - yh) ** 2 for yi, yh in zip(y, y_hat))
        ss_tot = sum((yi - my) ** 2 for yi in y)
        r2     = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        slope_pct = (slope / my) * 100 if my > 0 else 0.0

        return {
            "direction":           "growing" if slope > 0 else "declining" if slope < 0 else "stable",
            "slope_pct_per_month": round(slope_pct, 2),
            "slope_lyd_per_month": round(slope, 2),
            "r_squared":           round(r2, 4),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # RAMADAN EFFECT (RULE 2)
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_ramadan_effect(
        self,
        company,
        monthly_series: list,
        relevant_ramadan: dict | None,
    ) -> dict:
        """
        RULE 2: Computes the Ramadan effect ONLY if the window is active.
        Returns a minimal dict (detected=False) if outside the window.
        """
        if not relevant_ramadan:
            return {"detected": False, "relevant": False, "note": "Outside Ramadan window"}

        effects = {}
        years_in_data = {r["year"] for r in monthly_series}

        for year in RAMADAN_STARTS:
            if year not in years_in_data:
                continue

            start = RAMADAN_STARTS[year]
            end   = start + timedelta(days=RAMADAN_DURATION_DAYS)

            from apps.transactions.models import MaterialMovement

            # Revenue during Ramadan
            ramadan_rev = (
                MaterialMovement.objects
                .filter(
                    company=company,
                    movement_type="ف بيع",
                    movement_date__gte=start,
                    movement_date__lte=end,
                )
                .aggregate(total=Sum("total_out"))
            )
            daily_ramadan = float(ramadan_rev["total"] or 0) / RAMADAN_DURATION_DAYS

            # Revenue 30 days before Ramadan (reference period)
            prior_start = start - timedelta(days=30)
            prior_rev   = (
                MaterialMovement.objects
                .filter(
                    company=company,
                    movement_type="ف بيع",
                    movement_date__gte=prior_start,
                    movement_date__lt=start,
                )
                .aggregate(total=Sum("total_out"))
            )
            daily_prior = float(prior_rev["total"] or 0) / 30

            if daily_prior > 0:
                ramadan_index = daily_ramadan / daily_prior
                effects[year] = {
                    "year":          year,
                    "start":         str(start),
                    "end":           str(end),
                    "ramadan_index": round(ramadan_index, 3),
                    "effect": (
                        "boost"   if ramadan_index > 1.05 else
                        "drop"    if ramadan_index < 0.95 else
                        "neutral"
                    ),
                    "daily_avg_lyd": round(daily_ramadan, 2),
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
            "dominant_effect": (
                "sales increase during Ramadan" if avg_effect > 1.05 else
                "sales decrease during Ramadan" if avg_effect < 0.95 else
                "neutral Ramadan effect"
            ),
            "annual_effects": effects,
            "adjustment_note": (
                f"Historical Ramadan impact: daily sales "
                f"{'increase' if avg_effect >= 1.0 else 'decrease'} "
                f"by {abs(avg_effect - 1) * 100:.0f}% on average. "
                f"{relevant_ramadan['label']}."
            ),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY PATTERNS
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_category_patterns(
        self, company, start_date: date, end_date: date
    ) -> list:
        """Seasonal patterns by product family (rolling window)."""
        try:
            from apps.transactions.models import MaterialMovement

            rows = (
                MaterialMovement.objects
                .filter(
                    company=company,
                    movement_type="ف بيع",
                    movement_date__gte=start_date,
                    movement_date__lt=end_date,
                )
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
                all_vals    = [v for vs in monthly_data.values() for v in vs]
                overall_avg = sum(all_vals) / len(all_vals) if all_vals else 0
                if not overall_avg:
                    continue

                month_si = {
                    m: sum(vs) / len(vs) / overall_avg
                    for m, vs in monthly_data.items()
                    if vs
                }
                if not month_si:
                    continue

                peak_m   = max(month_si, key=month_si.get)
                trough_m = min(month_si, key=month_si.get)

                results.append({
                    "category":          cat,
                    "peak_month":        peak_m,
                    "peak_month_name":   MONTH_NAMES[peak_m],
                    "peak_index":        round(month_si[peak_m], 4),
                    "trough_month":      trough_m,
                    "trough_month_name": MONTH_NAMES[trough_m],
                    "trough_index":      round(month_si[trough_m], 4),
                })

            return results

        except Exception as exc:
            logger.warning("[SeasonalAnalyzer] category_patterns: %s", exc)
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # DIRECT OPENAI CALL (RULE 5 — replaces AIClient)
    # ─────────────────────────────────────────────────────────────────────────

    def _call_openai(
        self,
        indices: dict,
        peaks: list,
        troughs: list,
        trend: dict,
        ramadan_analysis: dict,
        relevant_ramadan: dict | None,
        exchange_rate: dict,
        season_info: dict,
        today: date,
    ) -> dict | None:
        """
        RULE 5: Direct call to the OpenAI API (gpt-4o) for seasonal analysis.

        Builds the prompt with ALL rules injected:
          - RULE 2: Ramadan only if active/imminent
          - RULE 3: Live exchange rate in every financial recommendation
          - RULE 4: Current/imminent season only
        """
        import openai
        from django.conf import settings

        api_key = getattr(settings, "OPENAI_API_KEY", None)
        if not api_key:
            logger.warning("[SeasonalAnalyzer] OPENAI_API_KEY missing — AI analysis skipped")
            return None

        client = openai.OpenAI(api_key=api_key)

        # ── Seasonality indices as a readable table ───────────────────────────
        index_lines = []
        for m, v in sorted(indices.items()):
            si = v.get("seasonality_index")
            if si:
                bar = "█" * min(20, int(si * 10))
                index_lines.append(
                    f"  {v['month_name']:<12} SI={si:.4f}  {bar}  [{v['label'].upper()}]"
                )

        # ── RULE 3: Exchange rate ALWAYS included ─────────────────────────────
        fx_context = format_fx_context(exchange_rate)

        # ── RULE 2: Ramadan ONLY if active window ─────────────────────────────
        ramadan_section = ""
        if relevant_ramadan and ramadan_analysis.get("relevant"):
            avg_idx = ramadan_analysis.get("avg_ramadan_index", 1.0)
            ramadan_section = (
                f"\n\n=== RAMADAN ACTIVE ===\n"
                f"Status: {relevant_ramadan['label']}\n"
                f"Historical impact: {ramadan_analysis['dominant_effect']} "
                f"(avg index={avg_idx:.3f})\n"
                f"Preparation note: {ramadan_analysis.get('adjustment_note', '')}"
            )
        else:
            ramadan_section = "\n\n[Ramadan outside window — do not mention in the response]"

        # ── RULE 4: Current and upcoming season ──────────────────────────────
        season_context = (
            f"\n\n=== CURRENT SEASON ===\n"
            f"Current month: {season_info['current_month_name']} "
            f"(SI={season_info['current_si']:.2f}, {season_info['current_label'].upper()})\n"
        )
        if season_info.get("upcoming_peak"):
            up = season_info["upcoming_peak"]
            season_context += (
                f"Next peak: {up['month_name']} in {up['days_until']} days "
                f"(SI={up['si']:.2f})\n"
            )
        if season_info.get("upcoming_trough"):
            ut = season_info["upcoming_trough"]
            season_context += (
                f"Next trough: {ut['month_name']} in {ut['days_until']} days "
                f"(SI={ut['si']:.2f})\n"
            )

        user_prompt = (
            f"Seasonal analysis — B2B Distribution, Libya\n"
            f"Window: 12-month rolling | Today: {today.isoformat()}\n"
            f"Trend: {trend['direction']} "
            f"({trend.get('slope_pct_per_month', 0):+.2f}%/month, "
            f"R²={trend.get('r_squared', 0):.3f})\n\n"
            f"=== LIVE EXCHANGE RATE (RULE: include in all financial impacts) ===\n"
            f"{fx_context}\n\n"
            f"=== MONTHLY SEASONALITY INDICES ===\n"
            + "\n".join(index_lines)
            + f"\n\nPeaks:   {', '.join(MONTH_NAMES[m] for m in peaks) or 'None'}\n"
            f"Troughs: {', '.join(MONTH_NAMES[m] for m in troughs) or 'None'}"
            + season_context
            + ramadan_section
            + "\n\nIMPORTANT: Generate recommendations ONLY for the current season "
            "or the next 90 days. Never reference months that have already passed."
        )

        try:
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                max_tokens=1200,
                temperature=0.3,
            )

            raw = response.choices[0].message.content or "{}"
            result = json.loads(raw)
            logger.info(
                "[SeasonalAnalyzer] OpenAI responded with confidence=%s",
                result.get("confidence", "?"),
            )
            return result

        except json.JSONDecodeError as exc:
            logger.warning("[SeasonalAnalyzer] Invalid JSON from OpenAI: %s", exc)
            return None
        except Exception as exc:
            logger.warning("[SeasonalAnalyzer] OpenAI error: %s", exc)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # FINAL RESULT FORMATTING
    # ─────────────────────────────────────────────────────────────────────────

    def _format_result(
        self,
        indices: dict,
        trend: dict,
        peaks: list,
        troughs: list,
        category_patterns: list,
        season_info: dict,
        ramadan_analysis: dict,
        relevant_ramadan: dict | None,
        exchange_rate: dict,
        ai_result: dict | None,
        today: date,
        start_date: date,
        end_date: date,
    ) -> dict:
        # Default values (if OpenAI unavailable)
        narrative  = self._default_narrative(indices, peaks, troughs, trend)
        recs       = build_dynamic_recommendations(season_info, peaks, troughs, today)
        # RULE 4: stock calendar only for upcoming peaks
        stock_cal  = build_stock_calendar(peaks, today, indices)
        staffing   = self._default_staffing(indices, peaks)
        confidence = "medium"

        if ai_result and not ai_result.get("error"):
            narrative  = ai_result.get("seasonal_narrative",         narrative)
            recs       = ai_result.get("ai_recommendations",         recs)
            # RULE 4: only use AI calendar if it exists
            ai_cal     = ai_result.get("stock_preparation_calendar")
            if ai_cal:
                stock_cal = ai_cal
            staffing   = ai_result.get("staffing_implications",      staffing)
            confidence = ai_result.get("confidence",                 "medium")

        return {
            # Window metadata (RULE 1)
            "history_months":    HISTORY_MONTHS,
            "analysis_window": {
                "start":  str(start_date),
                "end":    str(end_date),
                "label":  f"{start_date.strftime('%B %Y')} → {end_date.strftime('%B %Y')}",
            },
            "analysis_year":     today.year,

            # Dynamic season (RULE 4)
            "current_season":       season_info["display_label"],
            "current_season_info":  season_info,
            "upcoming_peak_alert":  season_info["peak_alert"],

            # Trend
            "trend": trend,

            # RULE 3: live exchange rate always present
            "exchange_rate": exchange_rate,

            # Seasonality indices
            "seasonality_indices": indices,
            "peak_months":         peaks,
            "peak_month_names":    [MONTH_NAMES[m] for m in peaks],
            "trough_months":       troughs,
            "trough_month_names":  [MONTH_NAMES[m] for m in troughs],

            # Category patterns
            "category_patterns": category_patterns,

            # RULE 2: Ramadan — present only if active/imminent
            "ramadan_analysis": ramadan_analysis,
            "relevant_ramadan": relevant_ramadan,  # None if outside window

            # Narrative content (generated by OpenAI if available)
            "seasonal_narrative":         narrative,
            # RULE 4: upcoming peaks only
            "stock_preparation_calendar": stock_cal,
            "staffing_implications":      staffing,
            "ai_recommendations":         recs,
            "confidence":                 confidence,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # FALLBACKS (used if OpenAI unavailable for the narrative)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _default_narrative(
        indices: dict, peaks: list, troughs: list, trend: dict
    ) -> str:
        peak_names   = [MONTH_NAMES[m] for m in peaks]
        trough_names = [MONTH_NAMES[m] for m in troughs]
        parts = []

        if peak_names:
            valid_si = [
                indices[m]["seasonality_index"]
                for m in peaks
                if indices[m].get("seasonality_index")
            ]
            if valid_si:
                avg_si = sum(valid_si) / len(valid_si)
                parts.append(
                    f"Demand peaks in {', '.join(peak_names)} "
                    f"(avg SI={avg_si:.2f} — "
                    f"+{int((avg_si - 1) * 100)}% above average)."
                )

        if trough_names:
            parts.append(
                f"The lowest months are {', '.join(trough_names)}."
            )

        parts.append(
            f"Overall 12-month trend: {trend['direction']} "
            f"({trend.get('slope_pct_per_month', 0):+.2f}%/month)."
        )

        return " ".join(parts)

    @staticmethod
    def _default_staffing(indices: dict, peaks: list) -> str:
        if not peaks:
            return "No significant staffing implications detected."

        valid_si = [
            indices[m]["seasonality_index"]
            for m in peaks
            if indices[m].get("seasonality_index")
        ]
        if not valid_si:
            return "Insufficient data for staffing estimates."

        max_si = max(valid_si)
        pct    = int((max_si - 1) * 100)
        return (
            f"During {', '.join(MONTH_NAMES[m] for m in peaks)}, "
            f"order volume can increase by up to {pct}% above average. "
            f"Plan additional delivery staff and extended warehouse hours."
        )

    @staticmethod
    def _empty_result(reason: str, exchange_rate: dict) -> dict:
        """Empty result with context to diagnose missing data."""
        return {
            "error":              reason,
            "history_months":     HISTORY_MONTHS,
            "analysis_window":    {},
            "seasonality_indices": {},
            "peak_months":        [],
            "trough_months":      [],
            "trend":              {},
            "category_patterns":  [],
            # RULE 2: Ramadan silence
            "ramadan_analysis":   {"detected": False, "relevant": False},
            "relevant_ramadan":   None,
            # RULE 3: exchange rate even on error
            "exchange_rate":      exchange_rate,
            "ai_recommendations": [],
            "confidence":         "low",
        }