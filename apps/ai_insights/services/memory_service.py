"""
apps/ai_insights/services/memory_service.py
--------------------------------------------
Mémoire conversationnelle intelligente pour le Decision Advisor.

Stratégie :
  - Window memory : derniers 8 échanges intacts (contexte immédiat)
  - Summary memory : résumé automatique des échanges anciens (LLM)
  - Entity memory : entités mémorisées (clients, produits, branches)
  - Reference resolution : "le premier client" → "شركة الاتحاد"

Déclencheurs de résumé :
  - Tous les SUMMARY_EVERY messages (défaut : 10)
  - Résumé fait en arrière-plan (fire-and-forget) pour ne pas bloquer

Stockage :
  - Redis via Django cache
  - TTL : 1 heure par défaut
  - Sérialisé en JSON
"""

import json
import logging
import re
import threading
from datetime import datetime
from typing import Optional

from django.core.cache import cache

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

MEMORY_TTL         = 3600  # 1 heure
WINDOW_SIZE        = 8     # Nombre d'échanges récents conservés intacts
SUMMARY_EVERY      = 10    # Résumer tous les N échanges
MAX_SUMMARY_LEN    = 500   # Longueur max du résumé (chars)
MAX_ENTITIES       = 15    # Entités max par type

# ── Prompts ───────────────────────────────────────────────────────────────────

SUMMARY_SYSTEM_PROMPT = """Tu es un assistant spécialisé dans la compression de conversations business.

Ta mission : résumer la conversation fournie en conservant UNIQUEMENT :
  1. Les décisions prises
  2. Les métriques citées (chiffres, dates, montants LYD)
  3. Les entités nommées (clients, produits, branches)
  4. Les questions non résolues
  5. Le contexte business actuel

Ne conserve PAS : les politesses, les explications génériques, les répétitions.

Retourne UNIQUEMENT ce JSON valide (pas de markdown) :
{
  "summary": "<2-3 phrases denses capturant l'essentiel>",
  "key_metrics": {"<nom>": "<valeur avec unité>"},
  "entities": {
    "clients":   ["<nom1>", "<nom2>"],
    "products":  ["<nom1>"],
    "branches":  ["<nom1>"],
    "periods":   ["<période>"]
  },
  "decisions_made": ["<décision 1>"],
  "open_questions":  ["<question non résolue>"],
  "topics_covered":  ["<topic1>", "<topic2>"]
}"""


# ── MemoryService ─────────────────────────────────────────────────────────────

class MemoryService:
    """
    Gestion intelligente de la mémoire conversationnelle.
    
    Usage :
        memory = MemoryService(conversation_id="uuid-...")
        
        # Avant de répondre
        context = memory.get_context_for_prompt()
        entities = memory.get_entity_memory()
        
        # Après avoir répondu
        memory.save_exchange(user_message, assistant_response)
    """

    def __init__(self, conversation_id: str, ttl: int = MEMORY_TTL):
        self.conversation_id = conversation_id
        self.ttl             = ttl

        # Clés Redis
        self._state_key    = f"mem:state:{conversation_id}"
        self._history_key  = f"mem:history:{conversation_id}"
        self._lock_key     = f"mem:lock:{conversation_id}"

    # ── Public API ────────────────────────────────────────────────────────────

    def get_context_for_prompt(self, max_chars: int = 2000) -> str:
        """
        Retourne le contexte mémorisé formaté pour injection dans le prompt système.
        
        Inclut :
          - Résumé des échanges passés (si disponible)
          - Entités mémorisées
          - Questions ouvertes
          - Échanges récents (fenêtre)
        """
        state   = self._load_state()
        history = self._load_recent_history(n=WINDOW_SIZE)

        parts = []

        # 1. Résumé historique
        if state.get("summary"):
            parts.append(f"[CONTEXTE PRÉCÉDENT] {state['summary']}")

        # 2. Métriques clés mémorisées
        if state.get("key_metrics"):
            metrics_str = " | ".join(
                f"{k}: {v}" for k, v in list(state["key_metrics"].items())[:5]
            )
            parts.append(f"[MÉTRIQUES CLÉS] {metrics_str}")

        # 3. Entités mémorisées
        entities = state.get("entities", {})
        if entities.get("clients"):
            parts.append(f"[CLIENTS ÉVOQUÉS] {', '.join(entities['clients'][:5])}")
        if entities.get("products"):
            parts.append(f"[PRODUITS ÉVOQUÉS] {', '.join(entities['products'][:5])}")
        if entities.get("branches"):
            parts.append(f"[BRANCHES ÉVOQUÉES] {', '.join(entities['branches'][:5])}")

        # 4. Questions ouvertes
        if state.get("open_questions"):
            parts.append(
                f"[QUESTIONS EN ATTENTE] {' | '.join(state['open_questions'][:2])}"
            )

        # 5. Échanges récents (fenêtre)
        if history:
            parts.append("[ÉCHANGES RÉCENTS]")
            for msg in history[-WINDOW_SIZE * 2:]:  # user + assistant par échange
                role    = "Manager" if msg["role"] == "user" else "WEEG"
                content = msg["content"]
                # Tronquer intelligemment
                if len(content) > 300:
                    content = content[:280] + "..."
                parts.append(f"  {role}: {content}")

        context = "\n".join(parts)

        # Limiter la taille totale
        if len(context) > max_chars:
            context = context[:max_chars - 3] + "..."

        return context

    def get_entity_memory(self) -> dict:
        """Retourne les entités mémorisées pour la résolution de références."""
        state = self._load_state()
        return state.get("entities", {})

    def get_reference_map(self) -> dict[str, str]:
        """
        Retourne le mapping référence → nom réel.
        Ex: {"premier": "شركة الاتحاد", "deuxième": "مؤسسة النور"}
        """
        state = self._load_state()
        return state.get("reference_map", {})

    def resolve_references(self, question: str) -> str:
        """
        Résout les références anaphoriques dans la question.
        
        Ex: "le premier client mentionné" → "le client شركة الاتحاد"
        """
        ref_map = self.get_reference_map()
        resolved = question

        for ref, real_name in ref_map.items():
            # Chercher le référent dans la question (insensible à la casse)
            pattern = rf"\b{re.escape(ref)}\b"
            if re.search(pattern, resolved, re.IGNORECASE):
                resolved = re.sub(
                    pattern,
                    f'"{real_name}"',
                    resolved,
                    flags=re.IGNORECASE
                )
                logger.debug(
                    "[Memory] Reference resolved: '%s' → '%s'",
                    ref, real_name
                )

        # Résoudre "eux", "ils", "ces clients" → liste des clients mémorisés
        entities = self.get_entity_memory()
        vague_refs = [
            r"\beux\b", r"\bils\b", r"\belles\b",
            r"\bces\s+clients\b", r"\bthese\s+customers\b",
            r"\bهم\b", r"\bهؤلاء\s+العملاء\b",
        ]
        clients = entities.get("clients", [])
        if clients:
            for vague_ref in vague_refs:
                if re.search(vague_ref, resolved, re.IGNORECASE):
                    clients_str = ", ".join(f'"{c}"' for c in clients[:3])
                    resolved += f" (référence aux clients : {clients_str})"
                    break

        return resolved

    def save_exchange(self, user_message: str, assistant_response: dict) -> None:
        """
        Sauvegarde un échange et met à jour la mémoire.
        
        Non-bloquant : le résumé est déclenché en arrière-plan si nécessaire.
        """
        # Ajouter à l'historique
        history = self._load_recent_history(n=200)
        history.append({
            "role":      "user",
            "content":   user_message,
            "timestamp": datetime.utcnow().isoformat(),
        })
        history.append({
            "role":      "assistant",
            "content":   assistant_response.get("answer", ""),
            "topic":     assistant_response.get("topic", "general"),
            "timestamp": datetime.utcnow().isoformat(),
        })
        self._save_history(history)

        # Mise à jour rapide des entités depuis la réponse
        state = self._load_state()
        state = self._quick_entity_update(state, assistant_response, user_message)
        self._save_state(state)

        # Déclencher le résumé en arrière-plan si nécessaire
        exchange_count = len(history) // 2  # user + assistant = 1 échange
        if exchange_count > 0 and exchange_count % SUMMARY_EVERY == 0:
            self._trigger_async_summarization(history)

    def clear(self) -> None:
        """Efface toute la mémoire de cette conversation."""
        cache.delete(self._state_key)
        cache.delete(self._history_key)
        logger.info("[Memory] Cleared for conversation=%s", self.conversation_id)

    def get_stats(self) -> dict:
        """Retourne les stats de la mémoire pour débogage."""
        state   = self._load_state()
        history = self._load_recent_history(n=200)
        return {
            "conversation_id":    self.conversation_id,
            "exchange_count":     len(history) // 2,
            "has_summary":        bool(state.get("summary")),
            "entity_counts":      {
                k: len(v) for k, v in state.get("entities", {}).items()
            },
            "reference_map_size": len(state.get("reference_map", {})),
            "open_questions":     len(state.get("open_questions", [])),
        }

    # ── Mise à jour rapide des entités ────────────────────────────────────────

    def _quick_entity_update(
        self,
        state:     dict,
        response:  dict,
        question:  str,
    ) -> dict:
        """
        Mise à jour légère des entités sans appel LLM.
        Extrait les entités depuis les résultats SQL et les followups.
        """
        entities = state.get("entities", {})
        ref_map  = state.get("reference_map", {})

        # Extraire les topics pour catégorisation
        topic = response.get("topic", "general")
        topics_covered = state.get("topics_covered", [])
        if topic and topic not in topics_covered:
            topics_covered.append(topic)
        state["topics_covered"] = topics_covered[-10:]  # Max 10

        # Les followups contiennent parfois des indices sur les entités
        followups = response.get("suggested_followups", [])
        for followup in followups:
            # Détecter les noms arabes dans les followups
            arabic_matches = re.findall(
                r'[\u0600-\u06FF][\u0600-\u06FF\s/\-]{3,40}[\u0600-\u06FF]',
                followup
            )
            for match in arabic_matches:
                match = match.strip()
                if len(match) >= 4 and match not in entities.get("clients", []):
                    # Heuristique : noms arabes longs = probablement des clients
                    pass  # On ne fait pas d'hypothèses sans certitude

        # Mise à jour des métriques clés depuis la réponse
        answer = response.get("answer", "")
        metrics = state.get("key_metrics", {})

        # Extraire les montants LYD mentionnés
        lyd_matches = re.findall(r'(\d[\d\s,\.]*)\s*LYD', answer, re.IGNORECASE)
        if lyd_matches and len(lyd_matches) <= 3:
            for i, match in enumerate(lyd_matches[:2]):
                clean = match.replace(" ", "").replace(",", "")
                try:
                    amount = float(clean)
                    if amount > 1000:  # Ignorer les petites valeurs
                        metrics[f"montant_{i+1}"] = f"{amount:,.0f} LYD"
                except ValueError:
                    pass

        state["entities"]    = entities
        state["reference_map"] = ref_map
        state["key_metrics"] = metrics

        return state

    # ── Résumé asynchrone ─────────────────────────────────────────────────────

    def _trigger_async_summarization(self, history: list) -> None:
        """
        Lance le résumé en arrière-plan via un thread daemon.
        Ne bloque pas la réponse à l'utilisateur.
        """
        def _do_summarize():
            try:
                self._perform_summarization(history)
            except Exception as exc:
                logger.warning(
                    "[Memory] Background summarization failed: %s", exc
                )

        thread = threading.Thread(target=_do_summarize, daemon=True)
        thread.start()
        logger.debug("[Memory] Background summarization triggered")

    def _perform_summarization(self, history: list) -> None:
        """
        Exécute le résumé LLM et met à jour l'état.
        Appelé en arrière-plan.
        """
        # Éviter la double exécution (simple lock Redis)
        lock_key = f"mem:sumlock:{self.conversation_id}"
        if cache.get(lock_key):
            return
        cache.set(lock_key, "1", timeout=30)

        try:
            from apps.ai_insights.client import AIClient, AIClientError

            client = AIClient()

            # Prendre les messages anciens (garder les WINDOW_SIZE derniers intacts)
            to_summarize = history[:-WINDOW_SIZE * 2]
            if len(to_summarize) < 4:  # Minimum 2 échanges pour résumer
                return

            conversation_text = "\n".join(
                f"{m['role'].upper()}: {m['content'][:400]}"
                for m in to_summarize
            )

            result = client.complete(
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                user_prompt=f"Résume cette conversation business :\n\n{conversation_text}",
                model="fast",
                max_tokens=600,
                analyzer="memory_summarizer",
            )

            if result and not result.get("error"):
                state = self._load_state()

                # Mettre à jour le résumé
                new_summary = result.get("summary", "")
                if new_summary:
                    state["summary"] = new_summary[:MAX_SUMMARY_LEN]

                # Fusionner les entités
                new_entities = result.get("entities", {})
                existing_entities = state.get("entities", {})
                state["entities"] = self._merge_entities(existing_entities, new_entities)

                # Mettre à jour le reference_map depuis les entités
                state["reference_map"] = self._build_reference_map(state["entities"])

                # Métriques clés
                new_metrics = result.get("key_metrics", {})
                existing_metrics = state.get("key_metrics", {})
                existing_metrics.update(new_metrics)
                state["key_metrics"] = {
                    k: v for k, v in list(existing_metrics.items())[-10:]
                }

                # Questions ouvertes
                state["open_questions"] = result.get("open_questions", [])[:3]

                # Topics couverts
                new_topics = result.get("topics_covered", [])
                existing_topics = state.get("topics_covered", [])
                all_topics = list(dict.fromkeys(existing_topics + new_topics))
                state["topics_covered"] = all_topics[-10:]

                self._save_state(state)

                # Compresser l'historique (garder seulement les WINDOW_SIZE derniers)
                compressed = history[-WINDOW_SIZE * 2:]
                self._save_history(compressed)

                logger.info(
                    "[Memory] Summarization complete for conversation=%s "
                    "entities=%s",
                    self.conversation_id,
                    {k: len(v) for k, v in state["entities"].items()}
                )

        finally:
            cache.delete(lock_key)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _merge_entities(existing: dict, new: dict) -> dict:
        """Fusionne deux dicts d'entités en dédupliquant."""
        merged = {}
        all_keys = set(existing.keys()) | set(new.keys())

        for key in all_keys:
            combined = list(dict.fromkeys(
                existing.get(key, []) + new.get(key, [])
            ))
            merged[key] = combined[:MAX_ENTITIES]

        return merged

    @staticmethod
    def _build_reference_map(entities: dict) -> dict[str, str]:
        """
        Construit le mapping référence → nom réel depuis les entités.
        
        Ex: clients = ["شركة الاتحاد", "مؤسسة النور"]
        → {"premier": "شركة الاتحاد", "first": "شركة الاتحاد",
            "deuxième": "مؤسسة النور", "second": "مؤسسة النور"}
        """
        ref_map = {}

        ordinals_by_position = {
            0: ["premier", "première", "first", "الأول", "الأولى"],
            1: ["deuxième", "second", "الثاني", "الثانية"],
            2: ["troisième", "third", "الثالث", "الثالثة"],
        }

        clients = entities.get("clients", [])
        for i, client_name in enumerate(clients[:3]):
            for ref in ordinals_by_position.get(i, []):
                ref_map[ref] = client_name
            # Aussi le nom partiel comme référence
            if " " in client_name:
                short_name = client_name.split()[0]
                if len(short_name) >= 3:
                    ref_map[short_name.lower()] = client_name

        return ref_map

    # ── Cache Operations ──────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            raw = cache.get(self._state_key)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.debug("[Memory] State load failed: %s", exc)
        return {
            "summary":       "",
            "key_metrics":   {},
            "entities":      {},
            "reference_map": {},
            "open_questions":[],
            "topics_covered":[],
        }

    def _save_state(self, state: dict) -> None:
        try:
            cache.set(
                self._state_key,
                json.dumps(state, ensure_ascii=False),
                timeout=self.ttl,
            )
        except Exception as exc:
            logger.debug("[Memory] State save failed: %s", exc)

    def _load_recent_history(self, n: int = WINDOW_SIZE) -> list:
        try:
            raw = cache.get(self._history_key)
            if raw:
                history = json.loads(raw) if isinstance(raw, str) else raw
                # Retourner les n derniers échanges (user+assistant = 2 messages par échange)
                return history[-(n * 2):]
        except Exception as exc:
            logger.debug("[Memory] History load failed: %s", exc)
        return []

    def _save_history(self, history: list) -> None:
        try:
            # Limiter la taille (max 200 messages = 100 échanges)
            if len(history) > 200:
                history = history[-200:]
            cache.set(
                self._history_key,
                json.dumps(history, ensure_ascii=False),
                timeout=self.ttl,
            )
        except Exception as exc:
            logger.debug("[Memory] History save failed: %s", exc)