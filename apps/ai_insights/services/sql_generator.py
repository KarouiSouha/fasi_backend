"""
apps/ai_insights/services/sql_generator.py
-------------------------------------------
Génère du SQL via LLM à partir d'une question en langage naturel.

Sécurité :
  - Uniquement des SELECT (jamais INSERT/UPDATE/DELETE/DROP)
  - Paramètres liés via %s (jamais de f-string dans le SQL)
  - company_id TOUJOURS injecté comme paramètre lié
  - Validation par SQLValidator avant toute exécution
"""

import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)


SQL_GENERATION_SYSTEM_PROMPT = """Tu es un expert SQL PostgreSQL spécialisé dans les ERP
de distribution libyens. Tu génères uniquement des requêtes SELECT sécurisées.

RÈGLES ABSOLUES :
1. UNIQUEMENT des requêtes SELECT — jamais INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE
2. Toujours inclure WHERE company_id = %(company_id)s dans chaque table
3. Utiliser %(param)s pour tous les paramètres variables (pas de f-string)
4. Limiter les résultats : toujours ajouter LIMIT (défaut 100, max 500)
5. Pour les agrégations sans GROUP BY → OK (SUM, COUNT, AVG globaux)
6. Pas de sous-requêtes corrélées complexes — préférer les CTEs (WITH)
7. Les noms de tables sont en snake_case Django : app_modelname
8. Ne JAMAIS exposer des données d'une autre company_id

FORMAT DE RÉPONSE — UNIQUEMENT ce JSON valide :
{
  "sql": "<requête SQL complète avec %(company_id)s>",
  "params": {"param_name": "valeur"},
  "explanation": "<1 phrase : ce que fait la requête>",
  "result_columns": ["col1", "col2", ...],
  "confidence": "high" | "medium" | "low",
  "requires_aggregation": true | false
}

Si la question est impossible à traduire en SQL sécurisé :
{
  "sql": null,
  "error": "<raison>",
  "confidence": "low"
}"""


class SQLGenerator:
    """
    Traduit une question en langage naturel vers SQL via un LLM.
    """

    def __init__(self):
        self._openai_key = getattr(settings, "OPENAI_API_KEY", "").strip()
        self._anthropic_key = getattr(settings, "ANTHROPIC_API_KEY", "").strip()
        self._model = getattr(settings, "AI_MODEL_SMART", "gpt-4o-mini")

    def generate(
        self,
        question: str,
        schema: str,
        company_id: int,
        conversation_history: list[dict] = None,
    ) -> dict:
        """
        Génère une requête SQL depuis une question naturelle.

        Returns:
            {
                "sql": str | None,
                "params": dict,
                "explanation": str,
                "result_columns": list,
                "confidence": str,
                "requires_aggregation": bool,
                "error": str | None,
            }
        """
        user_prompt = self._build_user_prompt(question, schema, company_id)
        messages = self._build_messages(user_prompt, conversation_history)

        raw = self._call_llm(messages)
        if not raw:
            return {"sql": None, "error": "LLM unavailable", "confidence": "low"}

        result = self._parse_response(raw)

        # Injection systématique du company_id dans les params
        if result.get("sql"):
            result.setdefault("params", {})
            result["params"]["company_id"] = company_id

        logger.info(
            "[SQLGenerator] Generated SQL for question='%s...' confidence=%s",
            question[:50],
            result.get("confidence", "?"),
        )
        return result

    def _build_user_prompt(self, question: str, schema: str, company_id: int) -> str:
        return f"""SCHÉMA:
{schema}

QUESTION EN LANGAGE NATUREL:
{question}

COMPANY_ID COURANT: {company_id}
(Utiliser %(company_id)s dans le SQL — sera substitué automatiquement)

Génère le SQL PostgreSQL optimal pour répondre à cette question.
Réponds UNIQUEMENT avec le JSON demandé."""

    def _build_messages(
        self,
        user_prompt: str,
        history: list[dict] | None,
    ) -> list[dict]:
        messages = []
        if history:
            # Injecter l'historique pour le contexte multi-turn
            for msg in history[-4:]:  # max 4 messages précédents
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _call_llm(self, messages: list[dict]) -> str | None:
        """Appelle OpenAI ou Anthropic selon la configuration."""

        # Priorité 1 : Anthropic (Claude — meilleur pour le SQL complexe)
        if self._anthropic_key:
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=self._anthropic_key)
                resp = client.messages.create(
                    model=getattr(settings, "AI_MODEL_SMART", "claude-sonnet-4-6"),
                    max_tokens=1000,
                    system=SQL_GENERATION_SYSTEM_PROMPT,
                    messages=messages,
                )
                return resp.content[0].text if resp.content else None
            except Exception as exc:
                logger.warning("[SQLGenerator] Anthropic failed: %s", exc)

        # Priorité 2 : OpenAI
        if self._openai_key:
            try:
                import openai
                client = openai.OpenAI(api_key=self._openai_key)
                full_messages = [
                    {"role": "system", "content": SQL_GENERATION_SYSTEM_PROMPT}
                ] + messages
                resp = client.chat.completions.create(
                    model=self._model,
                    max_tokens=1000,
                    temperature=0.1,  # Basse température pour SQL déterministe
                    response_format={"type": "json_object"},
                    messages=full_messages,
                )
                return resp.choices[0].message.content if resp.choices else None
            except Exception as exc:
                logger.warning("[SQLGenerator] OpenAI failed: %s", exc)

        return None

    @staticmethod
    def _parse_response(raw: str) -> dict:
        """Parse robuste de la réponse JSON du LLM."""
        if not raw:
            return {"sql": None, "error": "empty_response", "confidence": "low"}

        # Nettoyer les marqueurs markdown si présents
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]

        try:
            data = json.loads(clean.strip())
            return {
                "sql":                   data.get("sql"),
                "params":                data.get("params", {}),
                "explanation":           data.get("explanation", ""),
                "result_columns":        data.get("result_columns", []),
                "confidence":            data.get("confidence", "medium"),
                "requires_aggregation":  data.get("requires_aggregation", False),
                "error":                 data.get("error"),
            }
        except json.JSONDecodeError:
            # Tentative d'extraction du SQL brut en fallback
            sql_match = re.search(r"SELECT\s+.+?(?:;|$)", raw, re.DOTALL | re.IGNORECASE)
            if sql_match:
                return {
                    "sql":        sql_match.group(0).strip(),
                    "params":     {},
                    "explanation": "Extrait depuis réponse non-JSON",
                    "confidence": "low",
                    "error":      None,
                }
            return {"sql": None, "error": "parse_failed", "confidence": "low"}