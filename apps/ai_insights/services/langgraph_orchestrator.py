"""
apps/ai_insights/services/langgraph_orchestrator.py
-----------------------------------------------------
Orchestrateur LangGraph réel pour le pipeline RAG.

Remplace LangGraphWorkflow (keyword scoring) par un vrai StateGraph
avec nodes, edges conditionnels et state typé.

Architecture :
  router_node → [sql | vector | text_to_sql | hybrid] → merge_node
             → response_node → memory_node → END

Améliorations vs l'existant :
  - State machine explicite et debuggable
  - Fallback conditionnel T2S → SQL
  - Fusion intelligente de plusieurs sources
  - Métriques de latence par node
  - Arrêt propre en cas d'erreur

Dépendances :
  pip install langgraph langchain-core
"""

import logging
import time
from datetime import date
from typing import TypedDict, Optional, Any

logger = logging.getLogger(__name__)

# ── State Schema ──────────────────────────────────────────────────────────────

class RAGState(TypedDict, total=False):
    """
    État partagé entre tous les nodes du graph.
    
    total=False → tous les champs sont optionnels (facilite l'initialisation partielle)
    """
    # ── Input ────────────────────────────────────────────────────────────────
    question:           str
    company_id:         int
    company:            Any       # Company ORM instance
    conversation_id:    Optional[str]
    language:           str
    user_role:          str

    # ── Routing ──────────────────────────────────────────────────────────────
    intent:             str       # intent classifié
    complexity:         str       # "simple" | "complex" | "ambiguous"
    confidence:         float     # 0.0 → 1.0
    intent_scores:      dict      # scores bruts par intent
    start_date:         Optional[str]
    end_date:           Optional[str]

    # ── Entities ─────────────────────────────────────────────────────────────
    branch_names:       list
    customer_name:      str
    product_names:      list
    supplier_name:      str
    top_n:              int

    # ── Memory ───────────────────────────────────────────────────────────────
    resolved_question:  str       # question après résolution des références
    memory_context:     str       # contexte mémorisé injecté dans le prompt
    entity_memory:      dict      # entités mémorisées

    # ── Retrieval Results ─────────────────────────────────────────────────────
    sql_context:        Optional[dict]
    vector_context:     Optional[dict]
    t2s_context:        Optional[dict]
    analyzer_context:   Optional[dict]

    # ── Merged Context ────────────────────────────────────────────────────────
    final_context:      str
    primary_source:     str       # quelle source a fourni le contexte principal

    # ── Response ─────────────────────────────────────────────────────────────
    response:           Optional[dict]
    error:              Optional[str]

    # ── Metadata ─────────────────────────────────────────────────────────────
    steps_taken:        list[str]
    node_latencies:     dict[str, int]  # ms par node
    cache_hit:          bool


# ── Default State Factory ─────────────────────────────────────────────────────

def make_initial_state(
    question:        str,
    company,
    conversation_id: str = None,
    language:        str = "English",
    user_role:       str = "manager",
    memory_context:  str = "",
    entity_memory:   dict = None,
) -> RAGState:
    """Crée un état initial complet pour le graph."""
    return {
        "question":          question,
        "company_id":        company.id,
        "company":           company,
        "conversation_id":   conversation_id,
        "language":          language,
        "user_role":         user_role,

        "intent":            "",
        "complexity":        "ambiguous",
        "confidence":        0.0,
        "intent_scores":     {},
        "start_date":        None,
        "end_date":          None,

        "branch_names":      [],
        "customer_name":     "",
        "product_names":     [],
        "supplier_name":     "",
        "top_n":             5,

        "resolved_question": question,
        "memory_context":    memory_context,
        "entity_memory":     entity_memory or {},

        "sql_context":       None,
        "vector_context":    None,
        "t2s_context":       None,
        "analyzer_context":  None,

        "final_context":     "",
        "primary_source":    "none",

        "response":          None,
        "error":             None,

        "steps_taken":       [],
        "node_latencies":    {},
        "cache_hit":         False,
    }


# ── Node Helper : timing ──────────────────────────────────────────────────────

def _timed_step(state: RAGState, step_name: str):
    """Context manager pour mesurer la latence d'un node."""
    class Timer:
        def __init__(self):
            self.start = 0.0
            self.elapsed_ms = 0

        def __enter__(self):
            self.start = time.monotonic()
            return self

        def __exit__(self, *args):
            self.elapsed_ms = int((time.monotonic() - self.start) * 1000)
            latencies = dict(state.get("node_latencies", {}))
            latencies[step_name] = self.elapsed_ms
            state["node_latencies"] = latencies
            state.setdefault("steps_taken", []).append(step_name)
            logger.debug(
                "[Graph:%s] %dms", step_name, self.elapsed_ms
            )

    return Timer()


# ── Node 1 : Query Preprocessor ──────────────────────────────────────────────

def query_preprocessor_node(state: RAGState) -> RAGState:
    """
    Pré-traitement de la question :
      1. Résolution des références anaphoriques (via mémoire)
      2. Normalisation (suppression des doublons, correction typos simples)
    
    Ne touche PAS à la classification — c'est le rôle du router_node.
    """
    with _timed_step(state, "query_preprocessor"):
        question      = state["question"]
        entity_memory = state.get("entity_memory", {})

        # Résolution des références via la mémoire
        try:
            if state.get("conversation_id") and entity_memory:
                from apps.ai_insights.services.memory_service import MemoryService
                memory = MemoryService(state["conversation_id"])
                resolved = memory.resolve_references(question)
            else:
                resolved = question
        except Exception as exc:
            logger.debug("[graph:preprocessor] Reference resolution failed: %s", exc)
            resolved = question

        state["resolved_question"] = resolved

    return state


# ── Node 2 : Router ───────────────────────────────────────────────────────────

def router_node(state: RAGState) -> RAGState:
    """
    Classifie l'intent et extrait les entités.
    Utilise IntentClassifier (scoring pondéré) à la place du keyword scoring naïf.
    """
    with _timed_step(state, "router"):
        question = state.get("resolved_question") or state["question"]

        try:
            from apps.ai_insights.services.intent_classifier import IntentClassifier
            from apps.ai_insights.services.query_weaver_service import QueryWeaverService

            clf = IntentClassifier()
            result = clf.classify(question=question, company=state["company"])

            state["intent"]        = result["type"]
            state["confidence"]    = result["confidence"]
            state["complexity"]    = result["complexity"]
            state["intent_scores"] = result.get("scores", {})
            state["branch_names"]  = result.get("branch_names", [])
            state["customer_name"] = result.get("customer_name", "")
            state["product_names"] = result.get("product_names", [])
            state["supplier_name"] = result.get("supplier_name", "")
            state["top_n"]         = result.get("top_n", 5)

            # Date range
            date_range = result.get("date_range")
            if date_range:
                state["start_date"] = date_range.get("start")
                state["end_date"]   = date_range.get("end")
            else:
                # Défaut : année courante
                today = date.today()
                state["start_date"] = f"{today.year}-01-01"
                state["end_date"]   = today.isoformat()

        except Exception as exc:
            logger.warning("[graph:router] Classification failed: %s", exc)
            state["intent"]     = "sales"
            state["confidence"] = 0.3
            state["complexity"] = "ambiguous"

        logger.info(
            "[graph:router] intent=%s confidence=%.2f complexity=%s",
            state["intent"], state["confidence"], state["complexity"]
        )

    return state


def route_decision(state: RAGState) -> str:
    """
    Edge function : détermine le prochain node après le router.
    
    Logique de routing :
      - T2S direct si question complexe avec haute confiance
      - SQL pour les intents connus
      - Vector pour les questions analytiques/causales
      - Hybrid pour les questions analytiques globales
    """
    intent     = state.get("intent", "sales")
    complexity = state.get("complexity", "ambiguous")
    confidence = state.get("confidence", 0.0)

    # Questions complexes avec conditions croisées → T2S directement
    if complexity == "complex" and confidence >= 0.6:
        logger.debug("[graph:route] → text_to_sql (complex+confident)")
        return "text_to_sql"

    # Intents analytiques globaux → hybrid (SQL + Vector)
    if intent == "analytical":
        logger.debug("[graph:route] → hybrid (analytical)")
        return "hybrid"

    # Intents vectoriels / causaux
    if intent in ("vector", "naming_explanation"):
        logger.debug("[graph:route] → vector (intent=%s)", intent)
        return "vector"

    # Intents SQL connus
    SQL_INTENTS = {
        "sales", "aging", "inventory", "customers", "branches",
        "purchases", "margin", "customer_ranking", "branch_ranking",
        "branch_comparison", "branch_detail", "product_sales",
        "top_products", "monthly_sales", "category_sales",
        "customer_inactive_debt", "branch_movement_cross",
        "returns_sale", "returns_buy", "transfers", "damaged",
        "adjustments", "opening_stock", "all_movements", "top_purchased",
    }
    if intent in SQL_INTENTS:
        logger.debug("[graph:route] → sql (intent=%s)", intent)
        return "sql"

    # Fallback : SQL
    logger.debug("[graph:route] → sql (fallback)")
    return "sql"


# ── Node 3 : SQL Retrieval ────────────────────────────────────────────────────

def sql_node(state: RAGState) -> RAGState:
    """
    Récupère les données via SQLService (ORM Django prédéfini).
    Correspond aux 25 intents statiques de l'ancienne architecture.
    """
    with _timed_step(state, "sql"):
        try:
            from apps.ai_insights.services.retrieval_service import RetrievalService

            retrieval = RetrievalService()

            # Reconstruire la question enrichie avec les paramètres extraits
            question = state.get("resolved_question") or state["question"]

            ctx = retrieval.build_context(
                question=question,
                company=state["company"],
            )

            state["sql_context"]    = ctx
            state["primary_source"] = "sql"

        except Exception as exc:
            logger.warning("[graph:sql] Failed: %s", exc)
            state["sql_context"] = None

    return state


# ── Node 4 : Vector Retrieval ─────────────────────────────────────────────────

def vector_node(state: RAGState) -> RAGState:
    """
    Recherche sémantique via Qdrant.
    Utilisé pour les questions causales, analytiques, de conseil.
    """
    with _timed_step(state, "vector"):
        try:
            from apps.ai_insights.services.retrieval_service import RetrievalService

            retrieval = RetrievalService()
            question  = state.get("resolved_question") or state["question"]

            ctx = retrieval._build_vector(question, state["company"])
            state["vector_context"] = ctx

        except Exception as exc:
            logger.warning("[graph:vector] Failed: %s", exc)
            state["vector_context"] = None

    return state


# ── Node 5 : Text-to-SQL Retrieval ───────────────────────────────────────────

def text_to_sql_node(state: RAGState) -> RAGState:
    """
    Génère et exécute du SQL depuis la question naturelle.
    Utilisé pour les questions complexes avec conditions dynamiques.
    """
    with _timed_step(state, "text_to_sql"):
        try:
            from apps.ai_insights.services.text_to_sql_service import TextToSQLService

            # Rate limiting par company
            if not _check_t2s_rate_limit(state["company_id"]):
                logger.warning(
                    "[graph:t2s] Rate limit exceeded for company=%s",
                    state["company_id"]
                )
                state["t2s_context"] = None
                return state

            question = state.get("resolved_question") or state["question"]

            result = TextToSQLService().query(
                question=question,
                company=state["company"],
            )

            state["t2s_context"] = result if result.get("success") else None

            if result.get("success"):
                state["primary_source"] = "text_to_sql"
                logger.info(
                    "[graph:t2s] Success — %d rows in %dms",
                    result.get("row_count", 0),
                    result.get("execution_ms", 0)
                )
            else:
                logger.warning("[graph:t2s] Failed: %s", result.get("error"))

        except Exception as exc:
            logger.warning("[graph:text_to_sql] Failed: %s", exc)
            state["t2s_context"] = None

    return state


def t2s_fallback_decision(state: RAGState) -> str:
    """
    Edge function : si T2S échoue, basculer sur SQL.
    """
    if state.get("t2s_context") and state["t2s_context"].get("success"):
        return "merge"
    logger.info("[graph:t2s] Fallback to SQL")
    return "sql_fallback"


# ── Node 5b : SQL Fallback (après échec T2S) ─────────────────────────────────

def sql_fallback_node(state: RAGState) -> RAGState:
    """SQL retrieval en fallback après échec T2S."""
    return sql_node(state)


# ── Node 6 : Hybrid (SQL + Vector en parallèle) ──────────────────────────────

def hybrid_node(state: RAGState) -> RAGState:
    """
    Combine SQL et Vector en parallèle pour les questions analytiques complexes.
    Utilise ThreadPoolExecutor pour la parallélisation.
    """
    with _timed_step(state, "hybrid"):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _get_sql():
            try:
                from apps.ai_insights.services.retrieval_service import RetrievalService
                question = state.get("resolved_question") or state["question"]
                return "sql", RetrievalService().build_context(question, state["company"])
            except Exception as exc:
                logger.warning("[graph:hybrid/sql] %s", exc)
                return "sql", None

        def _get_vector():
            try:
                from apps.ai_insights.services.retrieval_service import RetrievalService
                question = state.get("resolved_question") or state["question"]
                return "vector", RetrievalService()._build_vector(question, state["company"])
            except Exception as exc:
                logger.warning("[graph:hybrid/vector] %s", exc)
                return "vector", None

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(_get_sql), executor.submit(_get_vector)]
            for future in as_completed(futures, timeout=15):
                try:
                    source, result = future.result()
                    if source == "sql":
                        state["sql_context"]    = result
                        state["primary_source"] = "sql"
                    else:
                        state["vector_context"] = result
                except Exception as exc:
                    logger.warning("[graph:hybrid] Future failed: %s", exc)

    return state


# ── Node 7 : Merge ────────────────────────────────────────────────────────────

def merge_node(state: RAGState) -> RAGState:
    """
    Fusionne les contextes disponibles.
    
    Priorité : T2S > SQL > Vector > Business Context
    Le résultat est un texte structuré pour injection dans le prompt LLM.
    """
    with _timed_step(state, "merge"):
        from apps.ai_insights.services.rag_service import RagService

        rag = RagService()

        # Sélectionner le contexte principal
        if state.get("t2s_context") and state["t2s_context"].get("success"):
            primary_ctx = state["t2s_context"]
            state["primary_source"] = "text_to_sql"
        elif state.get("sql_context"):
            primary_ctx = state["sql_context"]
            state["primary_source"] = "sql"
        elif state.get("vector_context"):
            primary_ctx = state["vector_context"]
            state["primary_source"] = "vector"
        else:
            primary_ctx = {
                "mode":             "llm_only",
                "business_summary": "Données non disponibles pour cette requête.",
            }
            state["primary_source"] = "llm_only"

        # Enrichir avec le contexte vectoriel si disponible (et non déjà utilisé)
        if (
            state.get("vector_context") and
            state["primary_source"] != "vector" and
            state["vector_context"].get("items")
        ):
            primary_ctx["vector_enrichment"] = [
                {"text": item.get("text", "")[:300], "score": item.get("score", 0)}
                for item in state["vector_context"]["items"][:3]
            ]

        # Formater le contexte
        try:
            final_context = rag._format(primary_ctx)
        except Exception as exc:
            logger.warning("[graph:merge] Format failed: %s", exc)
            final_context = str(primary_ctx)[:2000]

        state["final_context"] = final_context

    return state


# ── Node 8 : Semantic Cache Check ─────────────────────────────────────────────

def cache_check_node(state: RAGState) -> RAGState:
    """
    Vérifie si une réponse similaire est déjà en cache.
    Si oui, bypasse le LLM.
    """
    with _timed_step(state, "cache_check"):
        cache_key = _semantic_cache_key(
            state.get("resolved_question") or state["question"],
            state["company_id"],
        )

        try:
            from django.core.cache import cache
            cached_response = cache.get(cache_key)
            if cached_response:
                import json
                state["response"]   = json.loads(cached_response)
                state["cache_hit"]  = True
                state["steps_taken"].append("cache_hit")
                logger.info("[graph:cache] HIT for company=%s", state["company_id"])
        except Exception as exc:
            logger.debug("[graph:cache] Check failed: %s", exc)

    return state


def cache_hit_decision(state: RAGState) -> str:
    """Si le cache a répondu, sauter la génération LLM."""
    if state.get("cache_hit") and state.get("response"):
        return "memory"
    return "response"


# ── Node 9 : Response Generation ─────────────────────────────────────────────

def response_node(state: RAGState) -> RAGState:
    """
    Génère la réponse finale via LLM.
    Utilise le contexte fusionné + la mémoire conversationnelle.
    """
    with _timed_step(state, "response"):
        try:
            from apps.ai_insights.services.rag_service import RagService
            from apps.ai_insights.services.openai_service import OpenAIService

            rag    = RagService()
            openai = OpenAIService()

            # Construire le prompt avec toutes les sources
            business_context = state.get("memory_context", "")
            data_context     = state.get("final_context", "")

            combined_context = "\n\n".join(filter(None, [business_context, data_context]))

            system_prompt = rag._system_prompt()
            user_prompt   = rag._user_prompt(
                question=state.get("resolved_question") or state["question"],
                business_context=combined_context,
                retrieval={
                    "mode":    "merged",
                    "content": data_context,
                },
            )

            result = openai.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                analyzer="rag_langgraph",
                max_tokens=1200,
            )

            if result and not result.get("error"):
                state["response"] = result

                # Mettre en cache la réponse (5 min pour les questions factuelles)
                _cache_response(
                    state.get("resolved_question") or state["question"],
                    state["company_id"],
                    result,
                    ttl=300,
                )
            else:
                logger.warning("[graph:response] LLM returned error: %s", result)
                state["response"] = _build_error_response(state)

        except Exception as exc:
            logger.error("[graph:response] Failed: %s", exc, exc_info=True)
            state["response"] = _build_error_response(state)
            state["error"]    = str(exc)

    return state


# ── Node 10 : Memory Update ───────────────────────────────────────────────────

def memory_node(state: RAGState) -> RAGState:
    """
    Met à jour la mémoire conversationnelle après la réponse.
    Non-bloquant : la persistence est asynchrone.
    """
    with _timed_step(state, "memory"):
        if not state.get("conversation_id") or not state.get("response"):
            return state

        try:
            from apps.ai_insights.services.memory_service import MemoryService

            memory = MemoryService(state["conversation_id"])
            memory.save_exchange(
                user_message=state["question"],
                assistant_response=state["response"],
            )
        except Exception as exc:
            logger.debug("[graph:memory] Update failed: %s", exc)

    return state


# ── Utility Functions ─────────────────────────────────────────────────────────

def _check_t2s_rate_limit(company_id: int) -> bool:
    """Rate limiting pour Text-to-SQL : max 20 appels/heure/company."""
    try:
        from django.core.cache import cache
        key   = f"t2s_rate:{company_id}"
        count = cache.get(key, 0)
        if count >= 20:
            return False
        cache.set(key, count + 1, timeout=3600)
        return True
    except Exception:
        return True  # En cas d'erreur Redis, laisser passer


def _semantic_cache_key(question: str, company_id: int) -> str:
    """Clé de cache basée sur la question normalisée."""
    import hashlib
    import re as _re
    normalized = _re.sub(r'\s+', ' ', question.lower().strip())
    h = hashlib.md5(f"{company_id}:{normalized}".encode()).hexdigest()[:16]
    return f"rag_response:{h}"


def _cache_response(question: str, company_id: int, response: dict, ttl: int = 300) -> None:
    """Met la réponse en cache."""
    try:
        from django.core.cache import cache
        import json as _json
        key = _semantic_cache_key(question, company_id)
        cache.set(key, _json.dumps(response, ensure_ascii=False), timeout=ttl)
    except Exception:
        pass


def _build_error_response(state: RAGState) -> dict:
    """Construit une réponse de fallback en cas d'échec LLM."""
    intent = state.get("intent", "general")

    fallback_answers = {
        "sales":      "Les données de ventes sont disponibles dans le panel transactions.",
        "aging":      "Les créances sont visibles dans le panel finance.",
        "inventory":  "L'inventaire est disponible dans le panel stock.",
        "customers":  "Les informations clients sont dans le panel clients.",
        "purchases":  "Les achats sont visibles dans le panel approvisionnement.",
    }

    answer = fallback_answers.get(
        intent,
        "L'analyse IA est temporairement indisponible. Consultez vos dashboards."
    )

    return {
        "answer":              answer,
        "decision_needed":     False,
        "decision_card":       None,
        "suggested_followups": [],
        "urgency":             "low",
        "topic":               intent,
        "_fallback":           True,
    }


# ── Graph Builder ─────────────────────────────────────────────────────────────

def build_rag_graph():
    """
    Construit le StateGraph LangGraph complet.
    
    Flux :
      query_preprocessor
           ↓
        router ────────────────┐
           │                  │
    [conditional edges]        │
           │                  │
    ┌──────┼──────────┐       │
    ↓      ↓          ↓       │
   sql   vector  text_to_sql  │
    │      │          │       │
    │      │    [fallback?]   │
    │      │      ↓   ↓      │
    │      │    merge  sql   │
    │      │          ↓      │
    └──────┴──────── merge ←─┘
                      ↓
                  cache_check
                      ↓
              [cache_hit_decision]
                ↓           ↓
            response      memory
                ↓           ↓
            memory         END
                ↓
               END
    """
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        raise ImportError(
            "LangGraph non installé. Exécuter : pip install langgraph langchain-core"
        )

    graph = StateGraph(RAGState)

    # ── Ajouter les nodes ─────────────────────────────────────────────────────
    graph.add_node("query_preprocessor", query_preprocessor_node)
    graph.add_node("router",             router_node)
    graph.add_node("sql",                sql_node)
    graph.add_node("sql_fallback",       sql_fallback_node)
    graph.add_node("vector",             vector_node)
    graph.add_node("text_to_sql",        text_to_sql_node)
    graph.add_node("hybrid",             hybrid_node)
    graph.add_node("merge",              merge_node)
    graph.add_node("cache_check",        cache_check_node)
    graph.add_node("response",           response_node)
    graph.add_node("memory",             memory_node)

    # ── Entry point ───────────────────────────────────────────────────────────
    graph.set_entry_point("query_preprocessor")

    # ── Edges fixes ───────────────────────────────────────────────────────────
    graph.add_edge("query_preprocessor", "router")

    # ── Routing conditionnel depuis router ────────────────────────────────────
    graph.add_conditional_edges(
        "router",
        route_decision,
        {
            "sql":          "sql",
            "vector":       "vector",
            "text_to_sql":  "text_to_sql",
            "hybrid":       "hybrid",
        }
    )

    # ── SQL → merge ───────────────────────────────────────────────────────────
    graph.add_edge("sql",        "merge")
    graph.add_edge("sql_fallback","merge")

    # ── Vector → merge ────────────────────────────────────────────────────────
    graph.add_edge("vector", "merge")

    # ── Hybrid → merge ───────────────────────────────────────────────────────
    graph.add_edge("hybrid", "merge")

    # ── T2S → fallback conditionnel ───────────────────────────────────────────
    graph.add_conditional_edges(
        "text_to_sql",
        t2s_fallback_decision,
        {
            "merge":       "merge",
            "sql_fallback":"sql_fallback",
        }
    )

    # ── merge → cache_check → [response|memory] ───────────────────────────────
    graph.add_edge("merge", "cache_check")

    graph.add_conditional_edges(
        "cache_check",
        cache_hit_decision,
        {
            "response": "response",
            "memory":   "memory",
        }
    )

    # ── response → memory → END ───────────────────────────────────────────────
    graph.add_edge("response", "memory")
    graph.add_edge("memory",   END)

    compiled = graph.compile()
    logger.info("[LangGraph] RAG graph compiled successfully")
    return compiled


# ── Singleton Management ──────────────────────────────────────────────────────

_rag_graph_instance = None
_graph_lock = __import__("threading").Lock()


def get_rag_graph():
    """
    Retourne l'instance compilée du graph (singleton thread-safe).
    Le graph est compilé une seule fois au premier appel.
    """
    global _rag_graph_instance

    if _rag_graph_instance is not None:
        return _rag_graph_instance

    with _graph_lock:
        if _rag_graph_instance is None:
            try:
                _rag_graph_instance = build_rag_graph()
                logger.info("[LangGraph] Graph singleton created")
            except ImportError as exc:
                logger.error("[LangGraph] Cannot build graph: %s", exc)
                raise
            except Exception as exc:
                logger.error("[LangGraph] Graph build failed: %s", exc)
                raise

    return _rag_graph_instance


def reset_rag_graph():
    """Recompile le graph (utile après une mise à jour des nodes)."""
    global _rag_graph_instance
    with _graph_lock:
        _rag_graph_instance = None
    logger.info("[LangGraph] Graph singleton reset")