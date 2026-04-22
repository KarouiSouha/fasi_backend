"""
apps/ai_insights/services/sql_validator.py
-------------------------------------------
Couche de sécurité multi-niveaux avant exécution du SQL généré.

Niveaux de validation :
  1. Syntaxique    — mots-clés dangereux interdits
  2. Structurel    — uniquement SELECT, pas de sous-requêtes malveillantes
  3. Sémantique    — toutes les tables dans la whitelist
  4. Paramétrique  — company_id présent et correct
  5. Complexité    — limite les requêtes trop lourdes

Inspiré de la défense en profondeur (defense in depth).
"""

import re
import logging

logger = logging.getLogger(__name__)

# ── Mots-clés dangereux — rejet immédiat ──────────────────────────────────────

FORBIDDEN_KEYWORDS = [
    # DML
    r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b",
    # DDL
    r"\bDROP\b", r"\bALTER\b", r"\bCREATE\b", r"\bTRUNCATE\b",
    r"\bRENAME\b", r"\bMODIFY\b",
    # Exécution
    r"\bEXEC\b", r"\bEXECUTE\b", r"\bCALL\b",
    # Injection classique
    r"--\s*$",        # Commentaire de fin de ligne
    r"/\*.*?\*/",     # Commentaire multi-lignes
    r"\bxp_\w+",      # SQL Server stored procs
    r"\bPG_SLEEP\b",  # Time-based blind injection
    r"\bCOPY\b",      # PostgreSQL COPY (lecture fichiers)
    r"\bLO_\w+",      # PostgreSQL large objects
    r"\b\\\\COPY\b",  # psql COPY command
    # Élévation de privilèges
    r"\bGRANT\b", r"\bREVOKE\b",
    r"\bCREATE\s+USER\b", r"\bDROP\s+USER\b",
    # Information schema (éviter la reconnaissance)
    r"\binformation_schema\b",
    r"\bpg_catalog\b",
    r"\bpg_tables\b",
    r"\bpg_user\b",
]

# ── Tables autorisées (whitelist stricte) ─────────────────────────────────────

ALLOWED_TABLES = {
    "transactions_materialmovement",
    "aging_agingreceivable",
    "aging_agingsnapshot",
    "inventory_inventorysnapshotline",
    "customers_customer",
    "branches_branch",
    "companies_company",
    # Tables de jointure potentiellement nécessaires
    "auth_user",                   # uniquement pour COUNT si besoin
}

# ── Limites de complexité ─────────────────────────────────────────────────────

MAX_QUERY_LENGTH = 3000        # caractères
MAX_JOINS        = 5           # nombre de JOINs
MAX_SUBQUERIES   = 3           # nombre de sous-requêtes
MAX_LIMIT        = 500         # lignes retournées max


class SQLValidationError(Exception):
    """Levée quand la validation échoue — contient le motif de rejet."""
    def __init__(self, message: str, level: str = "security"):
        super().__init__(message)
        self.level = level


class SQLValidator:
    """
    Valide le SQL généré avant exécution.
    Lève SQLValidationError si une règle est violée.
    """

    def validate(self, sql: str, params: dict, company_id: int) -> str:
        """
        Valide et corrige le SQL si possible.

        Returns:
            SQL validé (potentiellement corrigé)

        Raises:
            SQLValidationError si validation impossible
        """
        if not sql or not sql.strip():
            raise SQLValidationError("SQL vide", level="structural")

        sql_clean = sql.strip()

        # Niveau 1 : Mots-clés dangereux
        self._check_forbidden_keywords(sql_clean)

        # Niveau 2 : Uniquement SELECT
        self._check_select_only(sql_clean)

        # Niveau 3 : Tables dans la whitelist
        self._check_allowed_tables(sql_clean)

        # Niveau 4 : company_id présent
        sql_clean = self._ensure_company_filter(sql_clean, company_id)

        # Niveau 5 : LIMIT présent et raisonnable
        sql_clean = self._ensure_limit(sql_clean)

        # Niveau 6 : Complexité
        self._check_complexity(sql_clean)

        logger.info("[SQLValidator] Validation OK — %d chars", len(sql_clean))
        return sql_clean

    # ── Niveau 1 : Mots-clés dangereux ───────────────────────────────────────

    def _check_forbidden_keywords(self, sql: str) -> None:
        sql_upper = sql.upper()
        for pattern in FORBIDDEN_KEYWORDS:
            if re.search(pattern, sql_upper, re.IGNORECASE | re.DOTALL):
                matched = re.search(pattern, sql, re.IGNORECASE | re.DOTALL)
                raise SQLValidationError(
                    f"Mot-clé interdit détecté : '{matched.group(0) if matched else pattern}'",
                    level="security",
                )

    # ── Niveau 2 : SELECT uniquement ─────────────────────────────────────────

    def _check_select_only(self, sql: str) -> None:
        first_word = sql.strip().split()[0].upper()
        if first_word not in ("SELECT", "WITH"):
            raise SQLValidationError(
                f"Seuls SELECT et WITH (CTE) sont autorisés. Reçu : '{first_word}'",
                level="security",
            )

    # ── Niveau 3 : Tables autorisées ─────────────────────────────────────────

    def _check_allowed_tables(self, sql: str) -> None:
        # Extraire les noms de tables depuis FROM et JOIN
        table_pattern = re.compile(
            r"(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            re.IGNORECASE,
        )
        found_tables = {m.group(1).lower() for m in table_pattern.finditer(sql)}

        unauthorized = found_tables - ALLOWED_TABLES
        if unauthorized:
            raise SQLValidationError(
                f"Tables non autorisées : {unauthorized}. "
                f"Autorisées : {ALLOWED_TABLES}",
                level="security",
            )

    # ── Niveau 4 : company_id dans le WHERE ───────────────────────────────────

    def _ensure_company_filter(self, sql: str, company_id: int) -> str:
        """
        Vérifie que company_id est filtré.
        Si absent, tente de l'injecter (pour les requêtes simples).
        """
        # Vérifie la présence du paramètre
        has_company_param = (
            "%(company_id)s" in sql or
            f"company_id = {company_id}" in sql or
            "company_id" in sql.lower()
        )

        if not has_company_param:
            logger.warning(
                "[SQLValidator] company_id absent — tentative d'injection automatique"
            )
            # Injection dans le WHERE existant
            if re.search(r"\bWHERE\b", sql, re.IGNORECASE):
                sql = re.sub(
                    r"\bWHERE\b",
                    "WHERE company_id = %(company_id)s AND",
                    sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                # Pas de WHERE → lever une erreur (trop risqué d'injecter sans context)
                raise SQLValidationError(
                    "La requête n'a pas de clause WHERE avec company_id. "
                    "Refus d'exécution pour éviter un accès cross-company.",
                    level="security",
                )

        return sql

    # ── Niveau 5 : LIMIT raisonnable ─────────────────────────────────────────

    def _ensure_limit(self, sql: str) -> str:
        """Ajoute LIMIT 100 si absent, plafonne à MAX_LIMIT."""
        limit_match = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)

        if not limit_match:
            # Ajouter LIMIT avant un éventuel ;
            sql = sql.rstrip(";").rstrip()
            sql += f"\nLIMIT 100;"
            logger.debug("[SQLValidator] LIMIT 100 ajouté automatiquement")
        else:
            current_limit = int(limit_match.group(1))
            if current_limit > MAX_LIMIT:
                sql = re.sub(
                    r"\bLIMIT\s+\d+",
                    f"LIMIT {MAX_LIMIT}",
                    sql,
                    flags=re.IGNORECASE,
                )
                logger.warning(
                    "[SQLValidator] LIMIT réduit de %d à %d",
                    current_limit,
                    MAX_LIMIT,
                )

        return sql

    # ── Niveau 6 : Complexité ─────────────────────────────────────────────────

    def _check_complexity(self, sql: str) -> None:
        if len(sql) > MAX_QUERY_LENGTH:
            raise SQLValidationError(
                f"Requête trop longue ({len(sql)} > {MAX_QUERY_LENGTH} chars)",
                level="complexity",
            )

        join_count = len(re.findall(r"\bJOIN\b", sql, re.IGNORECASE))
        if join_count > MAX_JOINS:
            raise SQLValidationError(
                f"Trop de JOINs ({join_count} > {MAX_JOINS})",
                level="complexity",
            )

        # Compter les sous-requêtes (SELECT imbriqués)
        subquery_count = len(re.findall(r"\(\s*SELECT", sql, re.IGNORECASE)) 
        if subquery_count > MAX_SUBQUERIES:
            raise SQLValidationError(
                f"Trop de sous-requêtes ({subquery_count} > {MAX_SUBQUERIES})",
                level="complexity",
            )