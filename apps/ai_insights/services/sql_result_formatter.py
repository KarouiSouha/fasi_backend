"""
apps/ai_insights/services/sql_result_formatter.py
---------------------------------------------------
Transforme les résultats SQL bruts en contexte structuré
pour injection dans le prompt RAG.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_ROWS_IN_PROMPT    = 50     # Lignes affichées dans le prompt
MAX_VALUE_LENGTH      = 100    # Longueur max d'une valeur texte


class SQLResultFormatter:
    """
    Formate les résultats d'une requête SQL pour le prompt LLM.
    """

    def format_for_prompt(
        self,
        execution_result: dict,
        sql: str,
        explanation: str,
        question: str,
    ) -> str:
        """
        Retourne un bloc texte structuré pour injection dans le prompt RAG.
        """
        rows        = execution_result.get("rows", [])
        columns     = execution_result.get("columns", [])
        row_count   = execution_result.get("row_count", 0)
        exec_ms     = execution_result.get("execution_ms", 0)
        truncated   = execution_result.get("truncated", False)

        lines = [
            "=== RÉSULTATS TEXT-TO-SQL ===",
            f"Question: {question}",
            f"SQL: {explanation}",
            f"Lignes retournées: {row_count}"
            + (" (résultats tronqués à 500 lignes)" if truncated else ""),
            f"Temps d'exécution: {exec_ms}ms",
            "",
        ]

        if not rows:
            lines.append("Aucun résultat trouvé pour cette requête.")
            return "\n".join(lines)

        # Cas 1 : Résultat agrégé (1 ligne) → format vertical
        if row_count == 1:
            lines.append("Résultat :")
            for col, val in rows[0].items():
                lines.append(f"  {col:<30} = {self._fmt_value(val)}")

        # Cas 2 : Peu de lignes (≤10) → tableau complet
        elif row_count <= 10:
            lines.append(self._format_table(columns, rows))

        # Cas 3 : Beaucoup de lignes → résumé statistique + top N
        else:
            lines.append(f"Top {min(MAX_ROWS_IN_PROMPT, row_count)} résultats :")
            lines.append(self._format_table(columns, rows[:MAX_ROWS_IN_PROMPT]))

            # Résumé statistique pour les colonnes numériques
            stats = self._compute_stats(rows, columns)
            if stats:
                lines.append("")
                lines.append("Statistiques :")
                for col, s in stats.items():
                    lines.append(
                        f"  {col}: total={s['total']:,.2f} | "
                        f"avg={s['avg']:,.2f} | "
                        f"min={s['min']:,.2f} | max={s['max']:,.2f}"
                    )

        return "\n".join(lines)

    def format_for_display(self, execution_result: dict) -> dict:
        """
        Format pour retour API (frontend peut afficher en tableau).
        """
        return {
            "columns":      execution_result.get("columns", []),
            "rows":         execution_result.get("rows", [])[:MAX_ROWS_IN_PROMPT],
            "row_count":    execution_result.get("row_count", 0),
            "execution_ms": execution_result.get("execution_ms", 0),
            "truncated":    execution_result.get("truncated", False),
        }

    def _format_table(self, columns: list, rows: list) -> str:
        """Formate en tableau ASCII lisible par le LLM."""
        if not rows or not columns:
            return "(vide)"

        # Calculer la largeur de chaque colonne
        col_widths = {
            col: max(len(str(col)), max(
                min(len(str(row.get(col, ""))), MAX_VALUE_LENGTH)
                for row in rows
            ))
            for col in columns
        }

        # Header
        header = " | ".join(str(col).ljust(col_widths[col]) for col in columns)
        separator = "-+-".join("-" * col_widths[col] for col in columns)

        table_lines = [header, separator]

        # Lignes de données
        for row in rows:
            line = " | ".join(
                str(self._fmt_value(row.get(col, "")))[:MAX_VALUE_LENGTH].ljust(col_widths[col])
                for col in columns
            )
            table_lines.append(line)

        return "\n".join(table_lines)

    @staticmethod
    def _fmt_value(val: Any) -> str:
        """Formate une valeur pour affichage — nombres avec séparateurs."""
        if val is None:
            return "NULL"
        if isinstance(val, float):
            return f"{val:,.2f}"
        if isinstance(val, int):
            return f"{val:,}"
        return str(val)

    @staticmethod
    def _compute_stats(rows: list, columns: list) -> dict:
        """Calcule des statistiques pour les colonnes numériques."""
        stats = {}
        for col in columns:
            values = [
                row[col] for row in rows
                if isinstance(row.get(col), (int, float)) and row[col] is not None
            ]
            if values and len(values) > 1:
                stats[col] = {
                    "total": sum(values),
                    "avg":   sum(values) / len(values),
                    "min":   min(values),
                    "max":   max(values),
                }
        return stats