"""
apps/ai_insights/client.py
--------------------------
Wrapper simplifié pour OpenAI uniquement.

RÈGLES :
  1. Seul ce fichier appelle openai.*
  2. Toutes les sorties sont JSON.
  3. Les noms/codes clients ne sont JAMAIS envoyés ici.
  4. time.sleep() est INTERDIT dans les vues Django synchrones.
     En cas de rate limit → on lève RateLimitError immédiatement
     pour que la vue retourne le fallback sans bloquer le worker.

Configuration dans .env (seulement ces variables sont utilisées) :
    OPENAI_API_KEY    = "sk-proj-..."
    AI_MODEL_SMART    = "gpt-4o-mini"     # ou gpt-4o, gpt-4-turbo, etc.
    AI_MODEL_FAST     = "gpt-4o-mini"
"""

import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Levée quand l'appel AI échoue — la vue doit retourner le fallback."""
    pass


class RateLimitError(AIClientError):
    """Sous-classe spécifique pour rate limit."""
    pass


class AIClient:
    """
    Client OpenAI uniquement – version simplifiée et plus légère.
    """

    # Modèles qui supportent response_format={type: "json_object"}
    _JSON_MODE_MODELS = {
        "gpt-4o", "gpt-4o-mini", "gpt-4o-2024-08-06",
        "gpt-4-turbo", "gpt-3.5-turbo-1106", "gpt-3.5-turbo-0125",
    }

    def __init__(self):
        self._api_key = getattr(settings, "OPENAI_API_KEY", "").strip()
        self._model_smart = getattr(settings, "AI_MODEL_SMART", "gpt-4o-mini")
        self._model_fast  = getattr(settings, "AI_MODEL_FAST",  "gpt-4o-mini")
        self._client = None

        if not self._api_key:
            raise AIClientError("OPENAI_API_KEY manquant ou vide dans .env")

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise AIClientError(
                    "Le paquet 'openai' n'est pas installé. Exécutez :\n"
                    "pip install openai"
                )
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

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

        # 3. Premier objet JSON dans le texte
        m = re.search(r"\{.*\}", raw, re.DOTALL | re.MULTILINE)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning("[AIClient] Échec parsing JSON. Début réponse : %s", raw[:300])
        return {"error": "parse_failed", "raw_preview": raw[:500]}

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

        Raises:
            RateLimitError   → rate limit → fallback immédiat
            AIClientError    → autre erreur
        """
        resolved_model = self._model_smart if model == "smart" else self._model_fast
        client = self._get_client()

        use_json_mode = resolved_model in self._JSON_MODE_MODELS

        effective_system = system_prompt
        if not use_json_mode:
            effective_system += "\n\nReturn ONLY valid JSON. No markdown, no preamble, no explanation."

        kwargs = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": effective_system},
                {"role": "user",   "content": user_prompt},
            ],
        }

        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            import time as _time
            start = _time.monotonic()

            response = client.chat.completions.create(**kwargs)

            latency_ms = int((_time.monotonic() - start) * 1000)
            usage = response.usage
            total_tokens = usage.total_tokens if usage else 0

            logger.info(
                "[AIClient] ✓ openai analyzer=%s model=%s tokens=%d latency=%dms",
                analyzer, resolved_model, total_tokens, latency_ms
            )

            content = response.choices[0].message.content or ""
            result = self._extract_json(content)

            self._log_usage(analyzer, resolved_model, total_tokens, company_id)

            return result

        except openai.RateLimitError as exc:
            logger.warning("[AIClient] OpenAI rate limit — fallback. analyzer=%s", analyzer)
            raise RateLimitError("OpenAI rate limit reached") from exc

        except openai.AuthenticationError as exc:
            raise AIClientError("Clé OPENAI_API_KEY invalide.") from exc

        except openai.BadRequestError as exc:
            # Cas où json_object n'est pas supporté → on retente sans
            if use_json_mode:
                logger.warning(
                    "[AIClient] json_object non supporté sur %s → retry sans mode JSON",
                    resolved_model
                )
                kwargs.pop("response_format", None)
                if not kwargs["messages"][0]["content"].endswith("Return ONLY valid JSON."):
                    kwargs["messages"][0]["content"] += (
                        "\n\nReturn ONLY valid JSON. No markdown, no preamble."
                    )
                try:
                    response = client.chat.completions.create(**kwargs)
                    content = response.choices[0].message.content or ""
                    return self._extract_json(content)
                except Exception as retry_exc:
                    raise AIClientError(f"Retry failed: {retry_exc}") from retry_exc
            raise AIClientError(str(exc)) from exc

        except Exception as exc:
            logger.error("[AIClient] OpenAI error analyzer=%s : %s", analyzer, exc)
            raise AIClientError(str(exc)) from exc

    @staticmethod
    def _log_usage(analyzer: str, model: str, tokens: int, company_id: str | None) -> None:
        try:
            from apps.ai_insights.models import AIUsageLog
            # Estimation conservative
            cost_usd = round((tokens / 1000) * 0.0003, 8)
            AIUsageLog.objects.create(
                analyzer=analyzer,
                model=model,
                tokens_used=tokens,
                cost_usd=cost_usd,
                company_id=company_id,
            )
        except Exception:
            pass  # logging ne doit jamais casser le flux