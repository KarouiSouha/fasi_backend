"""
apps/ai_insights/apps.py
-------------------------
Configuration de l'application Django ai_insights.

Améliorations :
  - Pré-compilation du graph LangGraph au démarrage (singleton)
  - Validation des variables d'environnement requises au démarrage
  - Logging de la configuration au démarrage
"""

import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class AiInsightsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name               = "apps.ai_insights"
    verbose_name       = "AI Insights"

    def ready(self):
        """
        Appelé une seule fois au démarrage du serveur Django.
        
        Actions :
          1. Valider la configuration AI
          2. Pré-compiler le graph LangGraph (évite le cold start)
          3. Logger la configuration active
        """
        # Éviter la double exécution en mode auto-reload
        import os
        if os.environ.get("RUN_MAIN") == "true" or not os.environ.get("RUN_MAIN"):
            self._validate_config()
            self._precompile_graph()
            self._log_config()

    def _validate_config(self):
        """Vérifie que les variables d'environnement requises sont présentes."""
        from django.conf import settings

        warnings = []

        if not getattr(settings, "OPENAI_API_KEY", "").strip():
            warnings.append("OPENAI_API_KEY non configuré (OpenAI indisponible)")

        if not getattr(settings, "OPENAI_API_KEY", "").strip():
            logger.error(
                "[AiInsights] CRITIQUE : Aucune clé API configurée. "
                "Définir OPENAI_API_KEY dans .env"
            )
        else:
            for w in warnings:
                logger.warning("[AiInsights] Config: %s", w)

        # Vérifier Redis (cache)
        try:
            from django.core.cache import cache
            cache.set("ai_insights:startup_check", "ok", timeout=10)
            cache.delete("ai_insights:startup_check")
        except Exception as exc:
            logger.warning(
                "[AiInsights] Redis non disponible : %s. "
                "Le cache et la mémoire conversationnelle seront désactivés.", exc
            )

    def _precompile_graph(self):
        """
        Pré-compile le graph LangGraph pour éviter la latence au premier appel.
        Exécuté en arrière-plan pour ne pas bloquer le démarrage.
        """
        import threading

        def _compile():
            try:
                from apps.ai_insights.services.langgraph_orchestrator import get_rag_graph
                graph = get_rag_graph()
                logger.info(
                    "[AiInsights] LangGraph RAG graph pre-compiled successfully"
                )
            except ImportError:
                logger.warning(
                    "[AiInsights] LangGraph non installé — graph non compilé. "
                    "Exécuter : pip install langgraph langchain-core"
                )
            except Exception as exc:
                logger.warning(
                    "[AiInsights] Graph pre-compilation failed (non-critical): %s", exc
                )

        thread = threading.Thread(target=_compile, daemon=True, name="langgraph-precompile")
        thread.start()

    def _log_config(self):
        """Log la configuration AI active au démarrage."""
        from django.conf import settings

        openai_key    = getattr(settings, "OPENAI_API_KEY",    "").strip()
        model_smart   = getattr(settings, "AI_MODEL_SMART",   "not configured")
        model_fast    = getattr(settings, "AI_MODEL_FAST",    "not configured")
        rag_enabled   = getattr(settings, "AI_RAG_ENABLED",   False)
        t2s_enabled   = getattr(settings, "AI_TEXT_TO_SQL_ENABLED", True)
        qdrant_url    = getattr(settings, "QDRANT_URL",       "").strip()

        providers = []
        if openai_key:
            providers.append(f"OpenAI ({openai_key[:8]}...)")

        logger.info(
            "[AiInsights] Startup config:\n"
            "  Providers   : %s\n"
            "  Model Smart : %s\n"
            "  Model Fast  : %s\n"
            "  RAG enabled : %s\n"
            "  T2S enabled : %s\n"
            "  Qdrant      : %s",
            ", ".join(providers) or "NONE",
            model_smart,
            model_fast,
            rag_enabled,
            t2s_enabled,
            qdrant_url or "not configured",
        )