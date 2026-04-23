"""
apps/ai_insights/monitoring.py
--------------------------------
Monitoring des performances LLM et du pipeline RAG.

Métriques collectées :
  - Appels LLM par analyzer : count, latence moyenne, erreurs
  - Cache hits / misses
  - Circuit breaker status
  - Coût estimé par période
  - Distribution des intents
  - T2S success rate

Stockage : Redis (Django cache) avec agrégation journalière.
Exposition : endpoint /api/ai-insights/monitoring/

Usage :
    from apps.ai_insights.monitoring import MetricsCollector
    
    MetricsCollector.record_llm_call("churn_predictor", "anthropic", 350, True)
    MetricsCollector.record_cache_hit("rag_response", True)
    MetricsCollector.record_intent("customer_ranking", 0.85)
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
from functools import wraps
from typing import Optional

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

METRICS_TTL     = 7 * 86400  # 7 jours de rétention
METRICS_PREFIX  = "weeg:metrics"

# Coût estimé par 1K tokens selon le modèle (USD)
COST_PER_1K = {
    "gpt-4o":                    0.005,
    "gpt-4o-mini":               0.0003,
    "claude-sonnet-4-6":         0.003,
    "claude-haiku-4-5-20251001": 0.00025,
    "claude-opus-4-6":           0.015,
}
DEFAULT_COST_PER_1K = 0.001


# ── MetricsCollector ──────────────────────────────────────────────────────────

class MetricsCollector:
    """
    Collecte et stocke les métriques du pipeline IA dans Redis.
    
    Toutes les méthodes sont des class methods (sans état).
    Les métriques sont agrégées par jour.
    """

    # ── LLM Calls ────────────────────────────────────────────────────────────

    @classmethod
    def record_llm_call(
        cls,
        analyzer:    str,
        provider:    str,      # "openai" | "anthropic"
        latency_ms:  int,
        success:     bool,
        tokens:      int = 0,
        model:       str = "",
        company_id:  str = None,
    ) -> None:
        """Enregistre un appel LLM avec ses métriques."""
        try:
            from django.core.cache import cache
            today = date.today().isoformat()

            # Métriques par analyzer
            key = f"{METRICS_PREFIX}:llm:{analyzer}:{today}"
            cls._increment_metrics(cache, key, {
                "calls":         1,
                "successes":     1 if success else 0,
                "errors":        0 if success else 1,
                "total_ms":      latency_ms,
                "total_tokens":  tokens,
                "total_cost_usd": round(tokens / 1000 * COST_PER_1K.get(model, DEFAULT_COST_PER_1K), 8),
            })

            # Métriques par provider
            key_provider = f"{METRICS_PREFIX}:provider:{provider}:{today}"
            cls._increment_metrics(cache, key_provider, {
                "calls":    1,
                "errors":   0 if success else 1,
                "total_ms": latency_ms,
            })

            # Métriques par company
            if company_id:
                key_company = f"{METRICS_PREFIX}:company:{company_id}:{today}"
                cls._increment_metrics(cache, key_company, {
                    "calls":          1,
                    "total_tokens":   tokens,
                    "total_cost_usd": round(
                        tokens / 1000 * COST_PER_1K.get(model, DEFAULT_COST_PER_1K), 8
                    ),
                })

        except Exception as exc:
            logger.debug("[Monitoring] record_llm_call failed: %s", exc)

    # ── Cache Hits ────────────────────────────────────────────────────────────

    @classmethod
    def record_cache_hit(cls, cache_type: str, hit: bool) -> None:
        """Enregistre un hit/miss de cache."""
        try:
            from django.core.cache import cache
            today = date.today().isoformat()
            key   = f"{METRICS_PREFIX}:cache:{cache_type}:{today}"
            cls._increment_metrics(cache, key, {
                "hits":   1 if hit else 0,
                "misses": 0 if hit else 1,
                "total":  1,
            })
        except Exception as exc:
            logger.debug("[Monitoring] record_cache_hit failed: %s", exc)

    # ── Intent Distribution ───────────────────────────────────────────────────

    @classmethod
    def record_intent(cls, intent: str, confidence: float) -> None:
        """Enregistre un intent détecté avec sa confiance."""
        try:
            from django.core.cache import cache
            today = date.today().isoformat()
            key   = f"{METRICS_PREFIX}:intent:{intent}:{today}"
            cls._increment_metrics(cache, key, {
                "count":            1,
                "total_confidence": round(confidence, 4),
            })
        except Exception as exc:
            logger.debug("[Monitoring] record_intent failed: %s", exc)

    # ── T2S Metrics ───────────────────────────────────────────────────────────

    @classmethod
    def record_t2s(
        cls,
        success:      bool,
        latency_ms:   int,
        row_count:    int = 0,
        validation_level: str = "",
    ) -> None:
        """Enregistre une exécution Text-to-SQL."""
        try:
            from django.core.cache import cache
            today = date.today().isoformat()
            key   = f"{METRICS_PREFIX}:t2s:{today}"
            cls._increment_metrics(cache, key, {
                "calls":      1,
                "successes":  1 if success else 0,
                "failures":   0 if success else 1,
                "total_ms":   latency_ms,
                "total_rows": row_count,
            })

            if not success and validation_level:
                key_fail = f"{METRICS_PREFIX}:t2s_failures:{validation_level}:{today}"
                cls._increment_metrics(cache, key_fail, {"count": 1})

        except Exception as exc:
            logger.debug("[Monitoring] record_t2s failed: %s", exc)

    # ── Circuit Breaker Status ────────────────────────────────────────────────

    @classmethod
    def record_circuit_event(
        cls,
        provider: str,
        event:    str,   # "opened" | "closed" | "half_open" | "rejected"
    ) -> None:
        """Enregistre un événement circuit breaker."""
        try:
            from django.core.cache import cache
            today = date.today().isoformat()
            key   = f"{METRICS_PREFIX}:circuit:{provider}:{today}"
            cls._increment_metrics(cache, key, {event: 1})
        except Exception as exc:
            logger.debug("[Monitoring] record_circuit_event failed: %s", exc)

    # ── Node Latencies (LangGraph) ────────────────────────────────────────────

    @classmethod
    def record_graph_execution(
        cls,
        steps_taken:     list[str],
        node_latencies:  dict[str, int],
        total_latency_ms: int,
        cache_hit:        bool,
        company_id:       str = None,
    ) -> None:
        """Enregistre les métriques d'une exécution complète du graph LangGraph."""
        try:
            from django.core.cache import cache
            today = date.today().isoformat()

            # Latences par node
            for node_name, latency in node_latencies.items():
                key = f"{METRICS_PREFIX}:node:{node_name}:{today}"
                cls._increment_metrics(cache, key, {
                    "count":    1,
                    "total_ms": latency,
                })

            # Exécution globale
            key = f"{METRICS_PREFIX}:graph:{today}"
            cls._increment_metrics(cache, key, {
                "executions":        1,
                "cache_hits":        1 if cache_hit else 0,
                "total_ms":          total_latency_ms,
                "steps_count":       len(steps_taken),
            })

        except Exception as exc:
            logger.debug("[Monitoring] record_graph_execution failed: %s", exc)

    # ── Query Methods ─────────────────────────────────────────────────────────

    @classmethod
    def get_daily_summary(cls, target_date: date = None) -> dict:
        """
        Retourne un résumé complet des métriques pour une date donnée.
        
        Returns:
            dict avec llm_stats, cache_stats, t2s_stats, intent_stats, graph_stats
        """
        if target_date is None:
            target_date = date.today()

        date_str = target_date.isoformat()

        try:
            from django.core.cache import cache

            result = {
                "date":         date_str,
                "llm_stats":    cls._get_llm_stats(cache, date_str),
                "cache_stats":  cls._get_cache_stats(cache, date_str),
                "t2s_stats":    cls._get_t2s_stats(cache, date_str),
                "intent_stats": cls._get_intent_stats(cache, date_str),
                "graph_stats":  cls._get_graph_stats(cache, date_str),
                "circuit_stats":cls._get_circuit_stats(cache, date_str),
            }

            return result

        except Exception as exc:
            logger.warning("[Monitoring] get_daily_summary failed: %s", exc)
            return {"date": date_str, "error": str(exc)}

    @classmethod
    def get_period_summary(cls, days: int = 7) -> dict:
        """Retourne les métriques agrégées sur une période."""
        summaries = []
        for i in range(days):
            d = date.today() - timedelta(days=i)
            summaries.append(cls.get_daily_summary(d))

        # Agréger
        total_calls   = sum(s.get("llm_stats", {}).get("total_calls", 0) for s in summaries)
        total_tokens  = sum(s.get("llm_stats", {}).get("total_tokens", 0) for s in summaries)
        total_cost    = sum(s.get("llm_stats", {}).get("total_cost_usd", 0.0) for s in summaries)
        total_errors  = sum(s.get("llm_stats", {}).get("total_errors", 0) for s in summaries)

        return {
            "period_days":      days,
            "total_calls":      total_calls,
            "total_tokens":     total_tokens,
            "total_cost_usd":   round(total_cost, 6),
            "total_errors":     total_errors,
            "error_rate_pct":   round(total_errors / total_calls * 100, 2) if total_calls else 0,
            "daily_summaries":  summaries,
        }

    @classmethod
    def get_analyzer_breakdown(cls, days: int = 7) -> list[dict]:
        """Retourne les métriques par analyzer sur une période."""
        try:
            from django.core.cache import cache

            # Collecter tous les keys pour les analyzers connus
            analyzers = [
                "kpi_analyzer", "anomaly_detector", "churn_predictor",
                "stock_optimizer", "predictor", "critical_detector",
                "hv_churn_outcome", "hv_churn_playbook",
                "rag_langgraph", "memory_summarizer",
                "risk_alert", "chat",
            ]

            breakdown = []
            for analyzer in analyzers:
                total = {"calls": 0, "successes": 0, "errors": 0,
                         "total_ms": 0, "total_tokens": 0, "total_cost_usd": 0.0}

                for i in range(days):
                    d = (date.today() - timedelta(days=i)).isoformat()
                    key = f"{METRICS_PREFIX}:llm:{analyzer}:{d}"
                    data = cls._load(cache, key)
                    for k, v in data.items():
                        total[k] = total.get(k, 0) + v

                if total["calls"] > 0:
                    breakdown.append({
                        "analyzer":       analyzer,
                        "calls":          total["calls"],
                        "successes":      total["successes"],
                        "errors":         total["errors"],
                        "error_rate_pct": round(total["errors"] / total["calls"] * 100, 1),
                        "avg_latency_ms": round(total["total_ms"] / total["calls"]),
                        "total_tokens":   total["total_tokens"],
                        "total_cost_usd": round(total.get("total_cost_usd", 0), 6),
                    })

            breakdown.sort(key=lambda x: -x["calls"])
            return breakdown

        except Exception as exc:
            logger.warning("[Monitoring] get_analyzer_breakdown failed: %s", exc)
            return []

    # ── Private Helpers ───────────────────────────────────────────────────────

    @classmethod
    def _increment_metrics(cls, cache, key: str, increments: dict) -> None:
        """Met à jour atomiquement les métriques pour une clé."""
        data = cls._load(cache, key)
        for k, v in increments.items():
            data[k] = round(data.get(k, 0) + v, 8)
        cls._save(cache, key, data)

    @staticmethod
    def _load(cache, key: str) -> dict:
        """Charge les données d'une clé Redis."""
        try:
            raw = cache.get(key)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            pass
        return {}

    @staticmethod
    def _save(cache, key: str, data: dict) -> None:
        """Sauvegarde les données dans Redis."""
        try:
            cache.set(key, json.dumps(data), timeout=METRICS_TTL)
        except Exception:
            pass

    @classmethod
    def _get_llm_stats(cls, cache, date_str: str) -> dict:
        """Agrège les stats LLM de tous les analyzers pour une date."""
        analyzers = [
            "kpi_analyzer", "anomaly_detector", "churn_predictor",
            "stock_optimizer", "predictor", "critical_detector",
            "hv_churn_outcome", "hv_churn_playbook", "rag_langgraph",
            "memory_summarizer", "risk_alert", "chat",
        ]

        totals = {"total_calls": 0, "total_successes": 0, "total_errors": 0,
                  "total_ms": 0, "total_tokens": 0, "total_cost_usd": 0.0}

        by_analyzer = {}
        for analyzer in analyzers:
            key  = f"{METRICS_PREFIX}:llm:{analyzer}:{date_str}"
            data = cls._load(cache, key)
            if data.get("calls", 0) > 0:
                by_analyzer[analyzer] = {
                    "calls":          data.get("calls", 0),
                    "errors":         data.get("errors", 0),
                    "avg_latency_ms": round(data.get("total_ms", 0) / data["calls"]) if data.get("calls") else 0,
                    "total_tokens":   data.get("total_tokens", 0),
                    "cost_usd":       round(data.get("total_cost_usd", 0), 6),
                }
                for k in ["calls", "total_ms", "total_tokens"]:
                    totals[f"total_{k}"] = totals.get(f"total_{k}", 0) + data.get(k, 0)
                totals["total_errors"]     += data.get("errors", 0)
                totals["total_cost_usd"]   += data.get("total_cost_usd", 0.0)

        totals["avg_latency_ms"] = (
            round(totals["total_ms"] / totals["total_calls"])
            if totals["total_calls"] else 0
        )
        totals["error_rate_pct"] = (
            round(totals["total_errors"] / totals["total_calls"] * 100, 2)
            if totals["total_calls"] else 0
        )
        totals["by_analyzer"] = by_analyzer
        return totals

    @classmethod
    def _get_cache_stats(cls, cache, date_str: str) -> dict:
        cache_types = ["rag_response", "biz_ctx", "seasonal", "churn", "stock"]
        result = {}
        for ct in cache_types:
            key  = f"{METRICS_PREFIX}:cache:{ct}:{date_str}"
            data = cls._load(cache, key)
            if data.get("total", 0) > 0:
                result[ct] = {
                    "hits":        data.get("hits", 0),
                    "misses":      data.get("misses", 0),
                    "hit_rate_pct":round(data.get("hits", 0) / data["total"] * 100, 1),
                }
        return result

    @classmethod
    def _get_t2s_stats(cls, cache, date_str: str) -> dict:
        key  = f"{METRICS_PREFIX}:t2s:{date_str}"
        data = cls._load(cache, key)
        if not data.get("calls"):
            return {}
        return {
            "calls":           data.get("calls", 0),
            "successes":       data.get("successes", 0),
            "failures":        data.get("failures", 0),
            "success_rate_pct":round(data.get("successes", 0) / data["calls"] * 100, 1),
            "avg_latency_ms":  round(data.get("total_ms", 0) / data["calls"]) if data.get("calls") else 0,
            "avg_rows":        round(data.get("total_rows", 0) / data["calls"], 1) if data.get("calls") else 0,
        }

    @classmethod
    def _get_intent_stats(cls, cache, date_str: str) -> dict:
        intents = [
            "sales", "aging", "inventory", "customers", "purchases",
            "margin", "analytical", "customer_ranking", "branch_ranking",
            "top_products", "monthly_sales", "text_to_sql",
        ]
        result = {}
        for intent in intents:
            key  = f"{METRICS_PREFIX}:intent:{intent}:{date_str}"
            data = cls._load(cache, key)
            if data.get("count", 0) > 0:
                result[intent] = {
                    "count":        data["count"],
                    "avg_confidence": round(
                        data.get("total_confidence", 0) / data["count"], 3
                    ),
                }
        return result

    @classmethod
    def _get_graph_stats(cls, cache, date_str: str) -> dict:
        key  = f"{METRICS_PREFIX}:graph:{date_str}"
        data = cls._load(cache, key)
        if not data.get("executions"):
            return {}
        return {
            "executions":        data.get("executions", 0),
            "cache_hit_rate_pct":round(
                data.get("cache_hits", 0) / data["executions"] * 100, 1
            ) if data.get("executions") else 0,
            "avg_latency_ms":    round(
                data.get("total_ms", 0) / data["executions"]
            ) if data.get("executions") else 0,
            "avg_steps":         round(
                data.get("steps_count", 0) / data["executions"], 1
            ) if data.get("executions") else 0,
        }

    @classmethod
    def _get_circuit_stats(cls, cache, date_str: str) -> dict:
        result = {}
        for provider in ["openai", "anthropic"]:
            key  = f"{METRICS_PREFIX}:circuit:{provider}:{date_str}"
            data = cls._load(cache, key)
            if data:
                result[provider] = data
        return result


# ── Monitoring Decorator ──────────────────────────────────────────────────────

def monitor_llm_call(analyzer: str, provider: str = "auto"):
    """
    Décorateur pour monitorer automatiquement les appels LLM.
    
    Usage :
        @monitor_llm_call("churn_predictor")
        def _call_ai(self, ...):
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start       = time.monotonic()
            success     = False
            tokens      = 0
            model       = ""
            company_id  = None

            # Extraire company_id depuis les kwargs si disponible
            company_id = str(kwargs.get("company_id", ""))

            try:
                result  = func(*args, **kwargs)
                success = not (isinstance(result, dict) and result.get("error"))
                return result
            except Exception as exc:
                raise
            finally:
                latency_ms = int((time.monotonic() - start) * 1000)
                MetricsCollector.record_llm_call(
                    analyzer=analyzer,
                    provider=provider,
                    latency_ms=latency_ms,
                    success=success,
                    tokens=tokens,
                    model=model,
                    company_id=company_id,
                )

        return wrapper
    return decorator