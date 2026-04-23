"""
apps/ai_insights/client.py
--------------------------
Wrapper OpenAI avec circuit breaker et retry automatique.

AMÉLIORATIONS v2 :
  - CircuitBreaker pour éviter les cascades d'échecs
  - Retry avec backoff exponentiel (3 tentatives max)
  - OpenAI avec circuit breaker intégré
  - Métriques de latence et tokens par appel
  - RateLimitError levée immédiatement (pas de sleep dans les vues)
  - Logging structuré avec correlation IDs

RÈGLES :
  1. Seul ce fichier appelle openai.*
  2. Toutes les sorties sont JSON.
  3. Les noms/codes clients ne sont JAMAIS envoyés ici.
  4. time.sleep() est INTERDIT dans les vues Django synchrones.
"""

import json
import logging
import re
import time
import threading
from enum import Enum
from functools import wraps
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────

class AIClientError(Exception):
    """Levée quand l'appel AI échoue — la vue doit retourner le fallback."""
    pass


class RateLimitError(AIClientError):
    """Sous-classe spécifique pour rate limit."""
    pass


class CircuitOpenError(AIClientError):
    """Levée quand le circuit breaker est ouvert."""
    pass


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED    = "closed"     # Normal — les appels passent
    OPEN      = "open"       # Bloqué — trop d'échecs récents
    HALF_OPEN = "half_open"  # Test — un appel autorisé pour vérifier


class CircuitBreaker:
    """
    Circuit Breaker thread-safe pour protéger les appels LLM.
    
    Paramètres :
        failure_threshold : nombre d'échecs consécutifs avant ouverture
        recovery_timeout  : secondes avant de passer en HALF_OPEN
        success_threshold : succès consécutifs pour repasser en CLOSED
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
        name: str = "default",
    ):
        self.failure_threshold  = failure_threshold
        self.recovery_timeout   = recovery_timeout
        self.success_threshold  = success_threshold
        self.name               = name

        self._lock              = threading.Lock()
        self._state             = CircuitState.CLOSED
        self._failure_count     = 0
        self._success_count     = 0
        self._last_failure_time: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._get_state()

    def _get_state(self) -> CircuitState:
        """Calcule l'état courant (peut passer OPEN → HALF_OPEN automatiquement)."""
        if self._state == CircuitState.OPEN:
            if (
                self._last_failure_time is not None and
                time.monotonic() - self._last_failure_time >= self.recovery_timeout
            ):
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                logger.info("[CircuitBreaker:%s] → HALF_OPEN (testing recovery)", self.name)
        return self._state

    def call(self, func, *args, **kwargs):
        """
        Exécute func si le circuit est fermé/half-open.
        Lève CircuitOpenError si le circuit est ouvert.
        """
        with self._lock:
            state = self._get_state()

        if state == CircuitState.OPEN:
            logger.warning("[CircuitBreaker:%s] OPEN — call rejected", self.name)
            raise CircuitOpenError(
                f"Circuit breaker '{self.name}' is OPEN. LLM temporarily unavailable."
            )

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except (RateLimitError, AIClientError, CircuitOpenError) as exc:
            self._on_failure()
            raise
        except Exception as exc:
            self._on_failure()
            raise AIClientError(str(exc)) from exc

    def _on_success(self):
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info("[CircuitBreaker:%s] → CLOSED (recovered)", self.name)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = max(0, self._failure_count - 1)

    def _on_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("[CircuitBreaker:%s] → OPEN (half-open test failed)", self.name)
            elif (
                self._state == CircuitState.CLOSED and
                self._failure_count >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                logger.warning(
                    "[CircuitBreaker:%s] → OPEN after %d failures",
                    self.name, self._failure_count
                )

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "name":              self.name,
                "state":             self._state.value,
                "failure_count":     self._failure_count,
                "success_count":     self._success_count,
                "last_failure_time": self._last_failure_time,
            }

    def reset(self):
        """Force la réinitialisation du circuit (pour les tests ou l'admin)."""
        with self._lock:
            self._state         = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
        logger.info("[CircuitBreaker:%s] manually reset to CLOSED", self.name)


# ── Retry Decorator ───────────────────────────────────────────────────────────

def with_retry(max_attempts: int = 3, base_delay: float = 1.0, backoff: float = 2.0):
    """
    Décorateur de retry avec backoff exponentiel.
    
    Ne retente PAS sur :
      - RateLimitError (pas de retry — retourne fallback immédiatement)
      - CircuitOpenError
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except (RateLimitError, CircuitOpenError):
                    raise  # Pas de retry
                except AIClientError as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        delay = base_delay * (backoff ** (attempt - 1))
                        logger.warning(
                            "[Retry] Attempt %d/%d failed — retrying in %.1fs: %s",
                            attempt, max_attempts, delay, exc
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            "[Retry] All %d attempts failed: %s",
                            max_attempts, exc
                        )
            raise last_exc
        return wrapper
    return decorator


# ── Singleton Circuit Breakers ────────────────────────────────────────────────

_openai_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60, name="openai")


# ── AIClient ──────────────────────────────────────────────────────────────────

class AIClient:
    """
    Client OpenAI avec circuit breaker et retry.
    
    Configuration dans .env :
      OPENAI_API_KEY = "sk-proj-..."
      AI_MODEL_SMART = "gpt-4o"
      AI_MODEL_FAST  = "gpt-4o-mini"
    """

    # Modèles OpenAI supportant json_object mode
    _OPENAI_JSON_MODELS = {
        "gpt-4o", "gpt-4o-mini", "gpt-4o-2024-08-06",
        "gpt-4-turbo", "gpt-3.5-turbo-1106", "gpt-3.5-turbo-0125",
    }

    def __init__(self):
        self._openai_key    = getattr(settings, "OPENAI_API_KEY",    "").strip()
        self._model_smart   = getattr(settings, "AI_MODEL_SMART",   "gpt-4o")
        self._model_fast    = getattr(settings, "AI_MODEL_FAST",    "gpt-4o-mini")

        self._openai_client = None

        # Valider que la clé OpenAI est disponible
        if not self._openai_key:
            raise AIClientError(
                "Clé OPENAI_API_KEY manquante. "
                "Vérifier la configuration dans .env"
            )

    # ── Clients lazily initialized ────────────────────────────────────────────

    def _get_openai(self):
        if self._openai_client is None:
            if not self._openai_key:
                raise AIClientError("OPENAI_API_KEY manquant")
            try:
                import openai
                self._openai_client = openai.OpenAI(api_key=self._openai_key)
            except ImportError:
                raise AIClientError("Package 'openai' non installé. Exécuter : pip install openai")
        return self._openai_client

    # ── Public API ────────────────────────────────────────────────────────────

    def complete(
        self,
        system_prompt: str,
        user_prompt:   str,
        model:         str = "fast",
        max_tokens:    int = 800,
        analyzer:      str = "unknown",
        company_id:    str = None,
    ) -> dict:
        """
        Envoie une requête à OpenAI et retourne toujours un dict JSON.
        
        Utilise un circuit breaker pour éviter les cascades d'échecs.
        
        Raises:
            RateLimitError   → rate limit → fallback immédiat
            CircuitOpenError → circuit ouvert → fallback immédiat
            AIClientError    → autre erreur (après retry)
        """
        resolved_model = self._model_smart if model == "smart" else self._model_fast

        # Appeler OpenAI avec circuit breaker
        try:
            result = _openai_breaker.call(
                self._complete_openai_with_retry,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=resolved_model,
                max_tokens=max_tokens,
                analyzer=analyzer,
                company_id=company_id,
            )
            return result
        except CircuitOpenError as exc:
            raise AIClientError("OpenAI provider is temporarily unavailable (circuit open)") from exc

    # ── OpenAI Implementation ─────────────────────────────────────────────────

    @with_retry(max_attempts=3, base_delay=0.5, backoff=2.0)
    def _complete_openai_with_retry(
        self,
        system_prompt: str,
        user_prompt:   str,
        model:         str,
        max_tokens:    int,
        analyzer:      str,
        company_id:    str,
    ) -> dict:
        return self._complete_openai(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            analyzer=analyzer,
            company_id=company_id,
        )

    def _complete_openai(
        self,
        system_prompt: str,
        user_prompt:   str,
        model:         str,
        max_tokens:    int,
        analyzer:      str,
        company_id:    str,
    ) -> dict:
        import openai as _openai

        # Sélectionner un modèle OpenAI valide
        openai_model  = self._resolve_openai_model(model)
        use_json_mode = openai_model in self._OPENAI_JSON_MODELS

        effective_system = system_prompt
        if not use_json_mode:
            effective_system += "\n\nReturn ONLY valid JSON. No markdown, no preamble."

        kwargs = {
            "model":       openai_model,
            "max_tokens":  max_tokens,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": effective_system},
                {"role": "user",   "content": user_prompt},
            ],
        }
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        client = self._get_openai()
        start  = time.monotonic()

        try:
            response = client.chat.completions.create(**kwargs)
        except _openai.RateLimitError as exc:
            raise RateLimitError("OpenAI rate limit") from exc
        except _openai.AuthenticationError as exc:
            raise AIClientError("Clé OPENAI_API_KEY invalide") from exc
        except _openai.BadRequestError as exc:
            # json_object non supporté → retry sans
            if use_json_mode:
                kwargs.pop("response_format", None)
                kwargs["messages"][0]["content"] += (
                    "\n\nReturn ONLY valid JSON. No markdown, no preamble."
                )
                try:
                    response = client.chat.completions.create(**kwargs)
                except Exception as retry_exc:
                    raise AIClientError(f"OpenAI retry failed: {retry_exc}") from retry_exc
            else:
                raise AIClientError(str(exc)) from exc
        except Exception as exc:
            raise AIClientError(f"OpenAI error: {exc}") from exc

        latency_ms   = int((time.monotonic() - start) * 1000)
        usage        = response.usage
        total_tokens = usage.total_tokens if usage else 0

        logger.info(
            "[AIClient] ✓ openai analyzer=%s model=%s tokens=%d latency=%dms",
            analyzer, openai_model, total_tokens, latency_ms
        )

        content = response.choices[0].message.content or ""
        result  = self._extract_json(content)

        self._log_usage(analyzer, openai_model, total_tokens, company_id)
        self._record_latency(analyzer, "openai", latency_ms)

        return result

    def _resolve_openai_model(self, model: str) -> str:
        """Mappe le modèle générique vers un modèle OpenAI valide."""
        model_lower = model.lower()

        # Si c'est déjà un modèle OpenAI
        if any(m in model_lower for m in ["gpt", "o1", "o3"]):
            return model

        # Fallback pour les demandes avec des noms d'autres fournisseurs
        # (compatibilité avec du code ancien qui référençerait d'autres providers)
        if any(m in model_lower for m in ["sonnet", "opus"]):
            return "gpt-4o"
        if any(m in model_lower for m in ["haiku"]):
            return "gpt-4o-mini"

        return self._model_fast

    # ── JSON Parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def _extract_json(raw: str) -> dict:
        """Extrait un dict JSON de manière robuste depuis la réponse brute."""
        if not raw:
            return {"error": "empty_response"}

        # 1. JSON direct
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            pass

        # 2. ```json ... ```
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 3. Premier { ... } dans le texte
        m = re.search(r"\{.*\}", raw, re.DOTALL | re.MULTILINE)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning("[AIClient] JSON parse failed. Preview: %s", raw[:300])
        return {"error": "parse_failed", "raw_preview": raw[:500]}

    # ── Logging & Metrics ─────────────────────────────────────────────────────

    @staticmethod
    def _log_usage(analyzer: str, model: str, tokens: int, company_id: str | None) -> None:
        """Enregistre la consommation de tokens en base."""
        try:
            from apps.ai_insights.models import AIUsageLog
            # Estimation du coût selon le modèle
            cost_per_1k = {
                "gpt-4o":        0.005,
                "gpt-4o-mini":   0.0003,
            }.get(model, 0.001)

            cost_usd = round((tokens / 1000) * cost_per_1k, 8)
            AIUsageLog.objects.create(
                analyzer=analyzer,
                model=model,
                tokens_used=tokens,
                cost_usd=cost_usd,
                company_id=company_id,
            )
        except Exception:
            pass  # Logging ne doit jamais casser le flux

    @staticmethod
    def _record_latency(analyzer: str, provider: str, latency_ms: int) -> None:
        """Enregistre la latence dans Redis pour monitoring."""
        try:
            from django.core.cache import cache
            import json as _json
            from datetime import date

            key = f"llm_latency:{analyzer}:{provider}:{date.today().isoformat()}"
            raw = cache.get(key)
            data = _json.loads(raw) if raw else {"count": 0, "total_ms": 0, "max_ms": 0}

            data["count"]    += 1
            data["total_ms"] += latency_ms
            data["max_ms"]    = max(data["max_ms"], latency_ms)
            data["avg_ms"]    = data["total_ms"] // data["count"]

            cache.set(key, _json.dumps(data), timeout=86400)
        except Exception:
            pass

    # ── Utilities ─────────────────────────────────────────────────────────────

    @classmethod
    def get_circuit_stats(cls) -> dict:
        """Retourne les stats du circuit breaker pour monitoring."""
        return {
            "openai": _openai_breaker.get_stats(),
        }

    @classmethod
    def reset_circuits(cls):
        """Remet le circuit breaker à zéro (admin uniquement)."""
        _openai_breaker.reset()