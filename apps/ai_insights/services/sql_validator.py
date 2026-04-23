"""
apps/ai_insights/services/sql_validator.py
-------------------------------------------
Validation SQL multi-niveaux avant exécution.

Niveaux :
  1. Syntaxique    — mots-clés DML/DDL dangereux interdits
  2. Structurel    — uniquement SELECT ou WITH (CTEs)
  3. Sémantique    — tables dans la whitelist stricte
  4. CTE Safety    — company_id dans CHAQUE CTE (nouveau)
  5. Paramétrique  — company_id dans le WHERE principal
  6. LIMIT         — toujours présent et raisonnable
  7. Complexité    — limites sur JOINs et sous-requêtes
  8. Injection     — patterns d'injection avancés

Défense en profondeur :
  - Whitelist de tables (pas blacklist)
  - Injection automatique de company_id si absent
  - Timeout PostgreSQL via SET LOCAL
"""

import re
import logging

logger = logging.getLogger(__name__)


# ── Constantes ────────────────────────────────────────────────────────────────

# Mots-clés dangereux — rejet immédiat
FORBIDDEN_PATTERNS = [
    # DML (modification de données)
    r"\bINSERT\s+INTO\b",
    r"\bUPDATE\s+\w+\s+SET\b",
    r"\bDELETE\s+FROM\b",

    # DDL (modification de structure)
    r"\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW|FUNCTION|PROCEDURE|TRIGGER)\b",
    r"\bALTER\s+(TABLE|DATABASE|SCHEMA)\b",
    r"\bCREATE\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW|FUNCTION|PROCEDURE|TRIGGER)\b",
    r"\bTRUNCATE\s+(TABLE)?\s*\w+",
    r"\bRENAME\s+\w+",

    # Exécution de code
    r"\bEXEC(UTE)?\s*\(",
    r"\bCALL\s+\w+\(",

    # Injection temporelle (time-based blind)
    r"\bPG_SLEEP\s*\(",
    r"\bSLEEP\s*\(",
    r"\bWAITFOR\s+DELAY\b",

    # Lecture de fichiers
    r"\bCOPY\s+\w+\s+FROM\b",
    r"\bCOPY\s+\w+\s+TO\b",
    r"\\COPY\b",
    r"\bLO_\w+\s*\(",
    r"\bPG_READ_FILE\s*\(",

    # Gestion des utilisateurs
    r"\bGRANT\s+\w+",
    r"\bREVOKE\s+\w+",
    r"\bCREATE\s+USER\b",
    r"\bDROP\s+USER\b",
    r"\bALTER\s+USER\b",

    # Information schema (reconnaissance)
    r"\binformation_schema\s*\.",
    r"\bpg_catalog\s*\.",
    r"\bpg_tables\b",
    r"\bpg_user\b",
    r"\bpg_shadow\b",

    # Stored procedures dangereuses
    r"\bxp_\w+",
    r"\bsp_\w+\s*\(",

    # Commentaires utilisés pour l'injection
    r"/\*[^*]*\*+(?:[^/*][^*]*\*+)*/",  # /* ... */
    r"--[^\n]*$",                          # -- commentaire fin de ligne

    # Stacking de requêtes
    r";\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC)",
]

# Tables autorisées (whitelist stricte)
ALLOWED_TABLES = frozenset({
    "transactions_movement",
    "aging_receivable",
    "aging_snapshot",
    "inventory_snapshot_line",
    "customers_customer",
    "branches_branch",
    "company",
    # Tables de référence (lecture seule)
    "auth_user",
    "django_content_type",
})

# Tables contenant company_id (doivent toujours être filtrées)
COMPANY_SCOPED_TABLES = frozenset({
    "transactions_movement",
    "aging_receivable",
    "aging_snapshot",
    "inventory_snapshot_line",
    "customers_customer",
    "company",
})

# Limites de complexité
MAX_QUERY_LENGTH = 4000    # caractères
MAX_JOINS        = 6       # nombre de JOINs
MAX_SUBQUERIES   = 4       # SELECT imbriqués
MAX_CTES         = 5       # CTEs (WITH clauses)
MAX_LIMIT        = 500     # lignes retournées max
DEFAULT_LIMIT    = 100     # LIMIT par défaut si absent


# ── Exceptions ────────────────────────────────────────────────────────────────

class SQLValidationError(Exception):
    """Levée quand la validation échoue."""

    def __init__(self, message: str, level: str = "security", sql_snippet: str = ""):
        super().__init__(message)
        self.level       = level       # "security" | "structural" | "complexity"
        self.sql_snippet = sql_snippet # Partie du SQL problématique

    def to_dict(self) -> dict:
        return {
            "error":       str(self),
            "level":       self.level,
            "sql_snippet": self.sql_snippet[:100] if self.sql_snippet else "",
        }


# ── SQLValidator ──────────────────────────────────────────────────────────────

class SQLValidator:
    """
    Valide et sécurise le SQL généré par le LLM avant exécution.
    
    Usage :
        validator = SQLValidator()
        try:
            safe_sql = validator.validate(sql, params, company_id)
            # Exécuter safe_sql
        except SQLValidationError as exc:
            # Rejeter — ne pas exécuter
            logger.warning("SQL rejected: %s", exc)
    """

    def validate(self, sql: str, params: dict, company_id: int) -> str:
        """
        Pipeline de validation complet.
        
        Returns:
            SQL validé et potentiellement corrigé (LIMIT ajouté, etc.)
        
        Raises:
            SQLValidationError si une règle de sécurité est violée
        """
        if not sql or not sql.strip():
            raise SQLValidationError("SQL vide fourni", level="structural")

        sql = sql.strip()

        # Exécuter les checks dans l'ordre de criticité
        self._check_1_forbidden_patterns(sql)
        self._check_2_select_only(sql)
        self._check_3_allowed_tables(sql)
        self._check_4_cte_company_filter(sql, company_id)
        sql = self._fix_5_company_filter(sql, company_id)
        sql = self._fix_6_limit(sql)
        self._check_7_complexity(sql)
        self._check_8_stacked_queries(sql)

        logger.info(
            "[SQLValidator] ✓ SQL validated — length=%d company_id=%d",
            len(sql), company_id
        )

        return sql

    # ── Check 1 : Mots-clés dangereux ────────────────────────────────────────

    def _check_1_forbidden_patterns(self, sql: str) -> None:
        """Vérifie qu'aucun mot-clé dangereux n'est présent."""
        for pattern in FORBIDDEN_PATTERNS:
            match = re.search(pattern, sql, re.IGNORECASE | re.DOTALL | re.MULTILINE)
            if match:
                snippet = sql[max(0, match.start()-20):match.end()+20]
                raise SQLValidationError(
                    f"Pattern dangereux détecté : '{match.group(0).strip()}'",
                    level="security",
                    sql_snippet=snippet,
                )

    # ── Check 2 : SELECT/WITH uniquement ─────────────────────────────────────

    def _check_2_select_only(self, sql: str) -> None:
        """La requête doit commencer par SELECT ou WITH (CTE)."""
        # Ignorer les espaces et commentaires en début
        sql_stripped = sql.strip()

        # Extraire le premier mot-clé SQL significatif
        first_token_match = re.match(
            r"^\s*(\w+)",
            sql_stripped,
            re.IGNORECASE,
        )
        if not first_token_match:
            raise SQLValidationError(
                "SQL ne commence pas par un mot-clé valide",
                level="structural",
            )

        first_token = first_token_match.group(1).upper()
        if first_token not in ("SELECT", "WITH"):
            raise SQLValidationError(
                f"Seuls SELECT et WITH (CTE) sont autorisés. Reçu : '{first_token}'",
                level="structural",
                sql_snippet=sql[:50],
            )

    # ── Check 3 : Tables dans la whitelist ────────────────────────────────────

    def _check_3_allowed_tables(self, sql: str) -> None:
        """Toutes les tables référencées doivent être dans la whitelist."""
        # Extraire les tables depuis FROM et JOIN (avec alias potentiels)
        table_pattern = re.compile(
            r"(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)(?:\s+(?:AS\s+)?[a-z_]\w*)?",
            re.IGNORECASE,
        )

        found_tables = set()
        for match in table_pattern.finditer(sql):
            table_name = match.group(1).lower()
            # Ignorer les CTEs (elles sont définies dans WITH, pas dans ALLOWED_TABLES)
            if not self._is_cte_name(sql, table_name):
                found_tables.add(table_name)

        unauthorized = found_tables - ALLOWED_TABLES
        if unauthorized:
            raise SQLValidationError(
                f"Tables non autorisées : {unauthorized}. "
                f"Tables autorisées : {sorted(ALLOWED_TABLES)}",
                level="security",
                sql_snippet=str(unauthorized),
            )

    def _is_cte_name(self, sql: str, table_name: str) -> bool:
        """Vérifie si table_name est un alias CTE défini dans WITH."""
        # Pattern : WITH nom_cte AS (
        cte_pattern = re.compile(
            rf"\b{re.escape(table_name)}\s+AS\s*\(",
            re.IGNORECASE,
        )
        return bool(cte_pattern.search(sql))

    # ── Check 4 : company_id dans chaque CTE ─────────────────────────────────

    def _check_4_cte_company_filter(self, sql: str, company_id: int) -> None:
        """
        Vérifie que chaque CTE qui accède à une table company-scoped
        inclut le filtre company_id.
        
        Nouvelle règle (FIX) — l'ancienne implémentation ne vérifiait que
        le WHERE principal et ignorait les CTEs imbriquées.
        """
        # Extraire les CTEs : WITH cte_name AS (...)
        cte_pattern = re.compile(
            r"(\w+)\s+AS\s*\(([^()]+(?:\([^()]*\)[^()]*)*)\)",
            re.IGNORECASE | re.DOTALL,
        )

        for cte_match in cte_pattern.finditer(sql):
            cte_name = cte_match.group(1)
            cte_body = cte_match.group(2)

            # Vérifier si ce CTE accède à une table scoped
            accesses_scoped = any(
                re.search(rf"\b{re.escape(t)}\b", cte_body, re.IGNORECASE)
                for t in COMPANY_SCOPED_TABLES
            )

            if not accesses_scoped:
                continue  # Ce CTE n'accède pas à des données company-scoped

            # Vérifier la présence du filtre company_id
            has_company_filter = (
                "%(company_id)s" in cte_body or
                f"company_id = {company_id}" in cte_body or
                re.search(r"company_id\s*=", cte_body, re.IGNORECASE) or
                re.search(r"s\.company_id\s*=", cte_body, re.IGNORECASE)  # via JOIN
            )

            if not has_company_filter:
                logger.warning(
                    "[SQLValidator] CTE '%s' accesses company-scoped table "
                    "without company_id filter",
                    cte_name
                )
                raise SQLValidationError(
                    f"Le CTE '{cte_name}' accède à des données scopées par company "
                    f"sans filtre company_id. Risque d'accès cross-company.",
                    level="security",
                    sql_snippet=cte_body[:200],
                )

    # ── Fix 5 : Injecter company_id si absent ─────────────────────────────────

    def _fix_5_company_filter(self, sql: str, company_id: int) -> str:
        """
        Vérifie la présence du filtre company_id dans le WHERE principal.
        Si absent (sur requêtes simples sans CTE), tente d'injecter.
        """
        has_company = (
            "%(company_id)s" in sql or
            f"company_id = {company_id}" in sql or
            re.search(r"\bcompany_id\s*=", sql, re.IGNORECASE)
        )

        if has_company:
            return sql

        # Tentative d'injection seulement sur requêtes simples (pas de CTE)
        has_cte = re.search(r"^\s*WITH\b", sql, re.IGNORECASE)
        if has_cte:
            # CTEs sans company_id → impossible d'injecter proprement
            raise SQLValidationError(
                "Requête WITH (CTE) sans filtre company_id détecté dans le corps principal. "
                "Chaque CTE doit inclure la clause company_id.",
                level="security",
            )

        # Injection dans une requête SELECT simple
        where_match = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
        if where_match:
            logger.info(
                "[SQLValidator] Auto-injecting company_id filter into WHERE"
            )
            sql = re.sub(
                r"\bWHERE\b",
                "WHERE company_id = %(company_id)s AND",
                sql,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            # Pas de WHERE : chercher l'emplacement avant GROUP BY / ORDER BY / LIMIT
            insertion_point = None
            for kw in ["GROUP BY", "ORDER BY", "LIMIT", "HAVING"]:
                m = re.search(rf"\b{kw}\b", sql, re.IGNORECASE)
                if m:
                    insertion_point = m.start()
                    break

            if insertion_point:
                sql = (
                    sql[:insertion_point].rstrip() +
                    " WHERE company_id = %(company_id)s " +
                    sql[insertion_point:]
                )
                logger.info("[SQLValidator] Auto-injected company_id WHERE before %s", kw)
            else:
                # Aucun point d'insertion sûr → rejeter
                raise SQLValidationError(
                    "Impossible d'injecter company_id : requête sans WHERE, GROUP BY, ORDER BY. "
                    "Refus d'exécution pour éviter l'exposition cross-company.",
                    level="security",
                )

        return sql

    # ── Fix 6 : LIMIT ─────────────────────────────────────────────────────────

    def _fix_6_limit(self, sql: str) -> str:
        """
        Ajoute LIMIT si absent, plafonne à MAX_LIMIT si trop élevé.
        
        Note : les agrégations globales (SUM, COUNT sans GROUP BY) n'ont pas
        besoin de LIMIT — on ne l'ajoute que si le résultat peut être multi-lignes.
        """
        has_group_by = bool(re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE))
        has_order_by = bool(re.search(r"\bORDER\s+BY\b", sql, re.IGNORECASE))
        limit_match  = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)

        # Requête d'agrégation pure (pas de GROUP BY, pas d'ORDER BY) → LIMIT non requis
        # Exemple : SELECT SUM(total_out), COUNT(*) FROM ... WHERE ...
        if not has_group_by and not has_order_by and not limit_match:
            is_pure_aggregation = self._is_pure_aggregation(sql)
            if is_pure_aggregation:
                return sql  # Pas de LIMIT pour les agrégations globales

        if limit_match:
            current_limit = int(limit_match.group(1))
            if current_limit > MAX_LIMIT:
                logger.warning(
                    "[SQLValidator] LIMIT reduced from %d to %d",
                    current_limit, MAX_LIMIT
                )
                sql = re.sub(
                    r"\bLIMIT\s+\d+",
                    f"LIMIT {MAX_LIMIT}",
                    sql,
                    flags=re.IGNORECASE,
                )
        else:
            # Ajouter LIMIT par défaut
            sql = sql.rstrip(";").rstrip()
            sql += f"\nLIMIT {DEFAULT_LIMIT};"
            logger.debug("[SQLValidator] Added default LIMIT %d", DEFAULT_LIMIT)

        return sql

    def _is_pure_aggregation(self, sql: str) -> bool:
        """Détecte si la requête retourne forcément 1 seule ligne (agrégation globale)."""
        # SELECT SUM/COUNT/AVG/MAX/MIN sans GROUP BY → 1 ligne max
        agg_funcs = re.findall(
            r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(",
            sql,
            re.IGNORECASE,
        )
        has_non_agg_select = re.search(
            r"SELECT\s+(?!.*\b(SUM|COUNT|AVG|MAX|MIN)\b.*FROM)(.+?)\s+FROM",
            sql,
            re.IGNORECASE | re.DOTALL,
        )
        return len(agg_funcs) > 0 and not re.search(r"\bGROUP\s+BY\b", sql, re.IGNORECASE)

    # ── Check 7 : Complexité ──────────────────────────────────────────────────

    def _check_7_complexity(self, sql: str) -> None:
        """Vérifie que la requête ne dépasse pas les limites de complexité."""
        if len(sql) > MAX_QUERY_LENGTH:
            raise SQLValidationError(
                f"Requête trop longue : {len(sql)} chars > {MAX_QUERY_LENGTH} max",
                level="complexity",
            )

        join_count = len(re.findall(r"\bJOIN\b", sql, re.IGNORECASE))
        if join_count > MAX_JOINS:
            raise SQLValidationError(
                f"Trop de JOINs : {join_count} > {MAX_JOINS} max",
                level="complexity",
            )

        subquery_count = len(re.findall(r"\(\s*SELECT\b", sql, re.IGNORECASE))
        if subquery_count > MAX_SUBQUERIES:
            raise SQLValidationError(
                f"Trop de sous-requêtes imbriquées : {subquery_count} > {MAX_SUBQUERIES} max",
                level="complexity",
            )

        cte_count = len(re.findall(r"\bAS\s*\(", sql, re.IGNORECASE))
        if cte_count > MAX_CTES:
            raise SQLValidationError(
                f"Trop de CTEs : {cte_count} > {MAX_CTES} max",
                level="complexity",
            )

    # ── Check 8 : Requêtes empilées (stacking) ────────────────────────────────

    def _check_8_stacked_queries(self, sql: str) -> None:
        """
        Détecte les tentatives d'empilage de requêtes via ';'.
        Ex: SELECT 1; DROP TABLE users;
        """
        # Chercher un ';' suivi d'un mot-clé SQL (hors fin de requête)
        stacking_pattern = re.compile(
            r";\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC|CALL|WITH)\b",
            re.IGNORECASE,
        )
        match = stacking_pattern.search(sql)
        if match:
            raise SQLValidationError(
                "Tentative d'empilage de requêtes détectée (query stacking)",
                level="security",
                sql_snippet=sql[max(0, match.start()-10):match.end()+20],
            )

    # ── Utilitaires ───────────────────────────────────────────────────────────

    def get_validation_report(self, sql: str, params: dict, company_id: int) -> dict:
        """
        Retourne un rapport détaillé de validation sans lever d'exception.
        Utile pour le débogage et les tests.
        """
        report = {
            "valid":      False,
            "checks":     [],
            "safe_sql":   None,
            "error":      None,
        }

        checks = [
            ("forbidden_patterns", self._check_1_forbidden_patterns),
            ("select_only",        self._check_2_select_only),
            ("allowed_tables",     self._check_3_allowed_tables),
        ]

        for check_name, check_fn in checks:
            try:
                check_fn(sql)
                report["checks"].append({"name": check_name, "status": "pass"})
            except SQLValidationError as exc:
                report["checks"].append({
                    "name":   check_name,
                    "status": "fail",
                    "error":  str(exc),
                    "level":  exc.level,
                })
                report["error"] = str(exc)
                return report

        try:
            safe_sql = self.validate(sql, params, company_id)
            report["valid"]    = True
            report["safe_sql"] = safe_sql
        except SQLValidationError as exc:
            report["error"] = str(exc)
            report["checks"].append({
                "name":   "full_validation",
                "status": "fail",
                "error":  str(exc),
                "level":  exc.level,
            })

        return report