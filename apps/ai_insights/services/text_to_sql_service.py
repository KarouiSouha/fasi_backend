"""
apps/ai_insights/services/text_to_sql_service.py
--------------------------------------------------
Service principal orchestrant le pipeline Text-to-SQL complet :
  1. SchemaExtractor → schéma pour le prompt
  2. SQLGenerator    → LLM génère le SQL
  3. SQLValidator    → sécurité multi-niveaux
  4. SQLExecutor     → exécution sécurisée
  5. SQLResultFormatter → résultat → contexte RAG

Usage depuis RetrievalService :
  result = TextToSQLService().query(
      question="Quels clients ont acheté plus de 50,000 LYD ce mois ?",
      company=company_obj,
  )
"""

import logging

from .schema_extractor import SchemaExtractor
from .sql_generator import SQLGenerator
from .sql_validator import SQLValidator, SQLValidationError
from .sql_executor import SQLExecutor, SQLExecutionError
from .sql_result_formatter import SQLResultFormatter

logger = logging.getLogger(__name__)


class TextToSQLError(Exception):
    """Erreur générique du pipeline Text-to-SQL."""
    pass


class TextToSQLService:
    """
    Pipeline complet Text-to-SQL.
    """

    def __init__(self):
        self.schema_extractor = SchemaExtractor()
        self.sql_generator    = SQLGenerator()
        self.sql_validator    = SQLValidator()
        self.sql_executor     = SQLExecutor()
        self.formatter        = SQLResultFormatter()

    def query(
        self,
        question: str,
        company,
        conversation_history: list[dict] = None,
        explain_plan: bool = False,
    ) -> dict:
        """
        Exécute le pipeline complet et retourne un contexte RAG enrichi.

        Returns:
            {
                "mode":         "text_to_sql",
                "success":      bool,
                "prompt_context": str,     ← pour injection dans RagService
                "display_data": dict,      ← pour le frontend
                "sql_generated": str,
                "explanation":  str,
                "confidence":   str,
                "error":        str | None,
                "pipeline_steps": list,    ← pour débogage
            }
        """
        company_id    = company.id
        pipeline_steps = []

        # ── Étape 1 : Extraction du schéma ──────────────────────────────────
        schema = self.schema_extractor.get_schema_for_prompt(company_id)
        pipeline_steps.append({"step": "schema_extraction", "status": "ok"})

        # ── Étape 2 : Génération SQL ─────────────────────────────────────────
        logger.info(
            "[TextToSQL] Generating SQL — company=%s question='%s...'",
            company_id, question[:60],
        )
        gen_result = self.sql_generator.generate(
            question=question,
            schema=schema,
            company_id=company_id,
            conversation_history=conversation_history,
        )
        pipeline_steps.append({
            "step":       "sql_generation",
            "status":     "ok" if gen_result.get("sql") else "failed",
            "confidence": gen_result.get("confidence"),
        })

        if not gen_result.get("sql"):
            return self._error_result(
                question=question,
                error=f"Génération SQL échouée : {gen_result.get('error', 'inconnu')}",
                pipeline_steps=pipeline_steps,
            )

        raw_sql = gen_result["sql"]

        # ── Étape 3 : Validation sécurité ────────────────────────────────────
        try:
            validated_sql = self.sql_validator.validate(
                sql=raw_sql,
                params=gen_result.get("params", {}),
                company_id=company_id,
            )
            pipeline_steps.append({"step": "validation", "status": "ok"})
        except SQLValidationError as exc:
            logger.warning(
                "[TextToSQL] Validation failed — company=%s level=%s : %s",
                company_id, exc.level, exc,
            )
            pipeline_steps.append({
                "step":   "validation",
                "status": "failed",
                "reason": str(exc),
                "level":  exc.level,
            })
            return self._error_result(
                question=question,
                error=f"Requête SQL rejetée ({exc.level}) : {exc}",
                pipeline_steps=pipeline_steps,
            )

        # ── Étape 4 : Exécution ──────────────────────────────────────────────
        params = gen_result.get("params", {})
        params["company_id"] = company_id

        try:
            exec_result = self.sql_executor.execute(
                sql=validated_sql,
                params=params,
                company_id=company_id,
                explain=explain_plan,
            )
            pipeline_steps.append({
                "step":       "execution",
                "status":     "ok",
                "rows":       exec_result["row_count"],
                "elapsed_ms": exec_result["execution_ms"],
            })
        except SQLExecutionError as exc:
            logger.error(
                "[TextToSQL] Execution failed — company=%s : %s",
                company_id, exc,
            )
            pipeline_steps.append({
                "step":   "execution",
                "status": "failed",
                "error":  str(exc),
            })
            return self._error_result(
                question=question,
                error=f"Erreur d'exécution : {exc}",
                pipeline_steps=pipeline_steps,
            )

        # ── Étape 5 : Formatage ──────────────────────────────────────────────
        prompt_context = self.formatter.format_for_prompt(
            execution_result=exec_result,
            sql=validated_sql,
            explanation=gen_result.get("explanation", ""),
            question=question,
        )
        display_data = self.formatter.format_for_display(exec_result)

        pipeline_steps.append({"step": "formatting", "status": "ok"})

        logger.info(
            "[TextToSQL] Pipeline complete — %d rows — company=%s",
            exec_result["row_count"], company_id,
        )

        return {
            "mode":            "text_to_sql",
            "success":         True,
            "prompt_context":  prompt_context,
            "display_data":    display_data,
            "sql_generated":   validated_sql,
            "params_used":     {k: v for k, v in params.items() if k != "company_id"},
            "explanation":     gen_result.get("explanation", ""),
            "confidence":      gen_result.get("confidence", "medium"),
            "row_count":       exec_result["row_count"],
            "execution_ms":    exec_result["execution_ms"],
            "explain_plan":    exec_result.get("explain"),
            "error":           None,
            "pipeline_steps":  pipeline_steps,
        }

    @staticmethod
    def _error_result(question: str, error: str, pipeline_steps: list) -> dict:
        return {
            "mode":           "text_to_sql",
            "success":        False,
            "prompt_context": f"Text-to-SQL échoué : {error}",
            "display_data":   {},
            "sql_generated":  None,
            "explanation":    "",
            "confidence":     "low",
            "row_count":      0,
            "execution_ms":   0,
            "error":          error,
            "pipeline_steps": pipeline_steps,
        }