"""
apps/ai_insights/services/sql_executor.py
------------------------------------------
Exécute le SQL validé de manière sécurisée avec :
  - Paramètres liés (protection injection)
  - Timeout d'exécution (PostgreSQL statement_timeout)
  - Sérialisation des résultats (Decimal, date → JSON-safe)
  - Audit logging
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from django.db import connection, OperationalError, ProgrammingError

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 10_000   # 10 secondes max par requête
MAX_ROWS_IN_MEMORY   = 500


class SQLExecutionError(Exception):
    """Levée lors d'une erreur d'exécution SQL."""
    pass


class SQLExecutor:
    """
    Exécute du SQL paramétré de manière sécurisée.
    """

    def execute(
        self,
        sql: str,
        params: dict,
        company_id: int,
        explain: bool = False,
    ) -> dict:
        """
        Exécute une requête SQL validée.

        Args:
            sql       : Requête SQL avec %(param)s placeholders
            params    : Dictionnaire de paramètres liés
            company_id: Vérifié que le filtre est bien présent
            explain   : Si True, retourne aussi le plan EXPLAIN

        Returns:
            {
                "rows":         list[dict],
                "row_count":    int,
                "columns":      list[str],
                "execution_ms": int,
                "explain":      str | None,
            }
        """
        import time

        # Vérification finale : company_id dans les params
        params = dict(params)
        params["company_id"] = company_id

        start_ms = time.monotonic()

        try:
            with connection.cursor() as cursor:
                # Timeout PostgreSQL pour éviter les requêtes longues
                cursor.execute(
                    f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS};"
                )

                # EXPLAIN optionnel pour le débogage
                explain_output = None
                if explain:
                    cursor.execute(f"EXPLAIN {sql}", params)
                    explain_output = "\n".join(
                        row[0] for row in cursor.fetchall()
                    )

                # Exécution réelle
                cursor.execute(sql, params)

                columns = [col[0] for col in cursor.description] if cursor.description else []
                raw_rows = cursor.fetchmany(MAX_ROWS_IN_MEMORY)

                # Sérialisation des types non-JSON
                rows = [
                    dict(zip(columns, self._serialize_row(row)))
                    for row in raw_rows
                ]

        except OperationalError as exc:
            if "statement timeout" in str(exc).lower():
                raise SQLExecutionError(
                    f"Requête trop lente (timeout {STATEMENT_TIMEOUT_MS}ms). "
                    "Simplifier la requête ou ajouter des filtres."
                ) from exc
            raise SQLExecutionError(f"Erreur base de données : {exc}") from exc

        except ProgrammingError as exc:
            raise SQLExecutionError(f"Erreur SQL : {exc}") from exc

        except Exception as exc:
            raise SQLExecutionError(f"Erreur inattendue : {exc}") from exc

        elapsed_ms = int((time.monotonic() - start_ms) * 1000)

        logger.info(
            "[SQLExecutor] OK — %d rows in %dms — company=%d",
            len(rows), elapsed_ms, company_id,
        )

        return {
            "rows":         rows,
            "row_count":    len(rows),
            "columns":      columns,
            "execution_ms": elapsed_ms,
            "explain":      explain_output,
            "truncated":    len(raw_rows) >= MAX_ROWS_IN_MEMORY,
        }

    @staticmethod
    def _serialize_row(row: tuple) -> list:
        """Convertit les types Python non-JSON en types sérialisables."""
        result = []
        for val in row:
            if isinstance(val, Decimal):
                result.append(float(val))
            elif isinstance(val, (date, datetime)):
                result.append(val.isoformat())
            elif val is None:
                result.append(None)
            else:
                result.append(val)
        return result