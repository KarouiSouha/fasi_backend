"""
apps/ai_insights/services/schema_extractor.py
----------------------------------------------
Extrait le schéma des modèles Django pertinents et le formate
pour injection dans le prompt Text-to-SQL.

Modèles couverts :
  - MaterialMovement  (transactions — cœur du système)
  - AgingReceivable   (créances)
  - InventorySnapshotLine (stock)
  - Customer          (clients)
  - Branch            (succursales)
"""

import logging
from functools import lru_cache
from django.db import models

logger = logging.getLogger(__name__)


# ── Constantes métier pour le contexte LLM ───────────────────────────────────

MOVEMENT_TYPES_CONTEXT = """
Types de mouvements (champ movement_type) :
  'ف بيع'          → Ventes (sorties stock, total_out = CA)
  'ف شراء'         → Achats (entrées stock, total_in = coût)
  'مردودات بيع'    → Retours clients (qty_in, total_in)
  'مردود شراء'     → Retours fournisseurs (qty_out, total_out)
  'نقل'            → Transferts inter-branches
  'ف تسوية المخ'   → Ajustements stock
  'ف.تالف'         → Produits abîmés/perdus
  'ف.أول المدة'    → Stock début de période
  'ادخال رئيسي'    → Entrées principales
  'اخراج رئيسي'   → Sorties principales
  'ف.عينات'        → Échantillons
"""

BUSINESS_RULES = """
Règles métier importantes :
  1. Toujours filtrer par company_id pour l'isolation des données
  2. Pour le CA (chiffre d'affaires) → utiliser movement_type='ف بيع' et SUM(total_out)
  3. Pour les achats → utiliser movement_type='ف شراء' et SUM(total_in)
  4. Les clients sont dans customer_name (peut être NULL — toujours exclure NULL)
    5. La branche est dans branch_id (FK vers branches_branch)
  6. Les dates sont dans movement_date (DateField)
  7. La devise est LYD (Dinar Libyen) — tous les montants en LYD
  8. qty_out = quantité sortie (ventes), qty_in = quantité entrée (achats)
"""

# ── Schéma des tables principales ────────────────────────────────────────────

SCHEMA_DEFINITIONS = {
    "transactions_movement": {
        "description": "Table principale des mouvements de stock et transactions",
        "columns": {
            "id":              ("UUID", "Clé primaire"),
            "company_id":      ("UUID", "FK → company.id — TOUJOURS filtrer par cette colonne"),
            "movement_type":   ("VARCHAR", "Type de mouvement (voir types arabes ci-dessus)"),
            "movement_date":   ("DATE",    "Date du mouvement"),
            "material_code":   ("VARCHAR", "Code produit (ex: EC0020, BDH110)"),
            "material_name":   ("VARCHAR", "Nom du produit"),
            "category":        ("VARCHAR", "Catégorie produit"),
            "qty_in":          ("DECIMAL", "Quantité entrante (achats, retours fournisseurs)"),
            "qty_out":         ("DECIMAL", "Quantité sortante (ventes, retours clients)"),
            "price_in":        ("DECIMAL", "Prix unitaire d'achat"),
            "price_out":       ("DECIMAL", "Prix unitaire de vente"),
            "total_in":        ("DECIMAL", "Montant total entrant (LYD)"),
            "total_out":       ("DECIMAL", "Montant total sortant (LYD) — CA pour les ventes"),
            "balance_price":   ("DECIMAL", "Prix de revient (coût moyen pondéré)"),
            "customer_name":   ("VARCHAR", "Nom du client (NULL pour certains mouvements)"),
            "customer_id":     ("UUID", "FK → customers_customer.id (nullable)"),
            "branch_id":       ("UUID", "FK → branches_branch.id (nullable)"),
        },
        "indexes": ["company_id", "movement_type", "movement_date", "material_code"],
        "sample_queries": [
            "-- CA mensuel : SELECT DATE_TRUNC('month', movement_date), SUM(total_out) FROM ... WHERE movement_type='ف بيع' GROUP BY 1",
            "-- Top clients : SELECT customer_name, SUM(total_out) FROM ... WHERE movement_type='ف بيع' AND customer_name IS NOT NULL GROUP BY 1 ORDER BY 2 DESC",
        ]
    },

    "aging_receivable": {
        "description": "Créances clients — vieillissement des impayés",
        "columns": {
            "id":          ("UUID", "Clé primaire"),
            "snapshot_id": ("UUID", "FK → aging_snapshot.id (dernier import)"),
            "company_id":  ("UUID", "FK → company.id"),
            "account":     ("VARCHAR", "Nom du compte client"),
            "account_code":("VARCHAR", "Code compte client"),
            "total":       ("DECIMAL", "Montant total dû (LYD)"),
            "current":     ("DECIMAL", "Montant non échu"),
            "d1_30":       ("DECIMAL", "1 à 30 jours de retard"),
            "d31_60":      ("DECIMAL", "31 à 60 jours"),
            "d61_90":      ("DECIMAL", "61 à 90 jours"),
            "d91_120":     ("DECIMAL", "91 à 120 jours"),
            "d121_150":    ("DECIMAL", "121 à 150 jours"),
            "d151_180":    ("DECIMAL", "151 à 180 jours"),
            "over_330":    ("DECIMAL", "Plus de 330 jours — risque de perte"),
        },
        "note": "Pour prendre le dernier import: joindre aging_snapshot via snapshot_id et trier par uploaded_at DESC",
        "join_hint": "JOIN aging_snapshot s ON s.id = snapshot_id WHERE s.company_id = %(company_id)s ORDER BY s.uploaded_at DESC LIMIT 1",
    },

    "aging_snapshot": {
        "description": "Sessions d'import aging (une ligne par fichier importé)",
        "columns": {
            "id":           ("UUID", "Clé primaire"),
            "company_id":   ("UUID", "FK → company.id"),
            "aging_year":   ("INTEGER", "Année fiscale extraite du fichier"),
            "report_date":  ("DATE", "Date du report"),
            "source_file":  ("VARCHAR", "Nom du fichier importé"),
            "uploaded_by_id": ("UUID", "FK user (nullable)"),
            "uploaded_at":  ("TIMESTAMP", "Date d'import"),
        },
    },

    "inventory_snapshot_line": {
        "description": "Lignes de stock — une ligne par produit par branche",
        "columns": {
            "id":              ("UUID", "Clé primaire"),
            "company_id":      ("UUID", "FK company — TOUJOURS filtrer"),
            "product_code":    ("VARCHAR", "Code produit"),
            "product_name":    ("VARCHAR", "Nom produit"),
            "branch_name":     ("VARCHAR", "Nom de la branche"),
            "quantity":        ("DECIMAL", "Quantité en stock"),
            "unit_cost":       ("DECIMAL", "Coût unitaire"),
            "line_value":      ("DECIMAL", "Valeur en stock (LYD)"),
            "product_category":("VARCHAR", "Catégorie"),
        },
        "note": "1 SKU × N branches = N lignes. Utiliser COUNT(DISTINCT product_code) pour le nb de SKUs uniques",
    },

    "customers_customer": {
        "description": "Référentiel clients",
        "columns": {
            "id":           ("UUID", "Clé primaire"),
            "company_id":   ("UUID", "FK company"),
            "name":         ("VARCHAR", "Nom du client"),
            "account_code": ("VARCHAR", "Code compte (lien avec aging)"),
            "address":      ("TEXT", "Adresse"),
            "phone":        ("VARCHAR", "Téléphone"),
            "area_code":    ("VARCHAR", "Zone géographique"),
            "email":        ("VARCHAR", "Email (nullable)"),
            "is_active":    ("BOOLEAN", "Client actif ou archivé"),
        }
    },

    "branches_branch": {
        "description": "Succursales actives",
        "columns": {
            "id":        ("INTEGER", "Clé primaire"),
            "name":      ("VARCHAR", "Nom de la branche"),
            "address":   ("VARCHAR", "Adresse"),
            "phone":     ("VARCHAR", "Téléphone"),
            "is_active": ("BOOLEAN", "Branche active"),
        }
    },

    "company": {
        "description": "Référentiel des sociétés",
        "columns": {
            "id":         ("UUID", "Clé primaire"),
            "name":       ("VARCHAR", "Nom de la société"),
            "is_active":  ("BOOLEAN", "Société active"),
            "created_at": ("TIMESTAMP", "Date de création"),
            "updated_at": ("TIMESTAMP", "Dernière mise à jour"),
        }
    },
}


class SchemaExtractor:
    """
    Génère une représentation textuelle du schéma de base de données
    optimisée pour la génération SQL par un LLM.
    """

    @lru_cache(maxsize=1)
    def get_schema_for_prompt(self, company_id: int = None) -> str:
        """
        Retourne le schéma complet formaté pour injection dans un prompt LLM.
        Résultat mis en cache (schéma stable entre les requêtes).
        """
        lines = [
            "=== SCHÉMA DE BASE DE DONNÉES (PostgreSQL) ===",
            "",
            MOVEMENT_TYPES_CONTEXT,
            BUSINESS_RULES,
            "",
            "=== TABLES DISPONIBLES ===",
            "",
        ]

        for table_name, schema in SCHEMA_DEFINITIONS.items():
            lines.append(f"TABLE: {table_name}")
            lines.append(f"Description: {schema['description']}")
            lines.append("Colonnes:")

            for col_name, (col_type, col_desc) in schema["columns"].items():
                lines.append(f"  {col_name:<20} {col_type:<10} -- {col_desc}")

            if "indexes" in schema:
                lines.append(f"Index: {', '.join(schema['indexes'])}")

            if "note" in schema:
                lines.append(f"Note: {schema['note']}")

            if "join_hint" in schema:
                lines.append(f"Join pattern: {schema['join_hint']}")

            if "sample_queries" in schema:
                lines.append("Exemples:")
                for sq in schema["sample_queries"]:
                    lines.append(f"  {sq}")

            lines.append("")

        if company_id:
            lines.append(f"CONTEXTE: company_id courant = {company_id}")
            lines.append("RÈGLE ABSOLUE: Toujours inclure WHERE company_id = {company_id}")
            lines.append("")

        return "\n".join(lines)

    def get_table_names(self) -> list[str]:
        """Retourne la liste des tables autorisées (whitelist de sécurité)."""
        return list(SCHEMA_DEFINITIONS.keys())

    def get_columns_for_table(self, table_name: str) -> list[str]:
        """Retourne les colonnes d'une table — pour validation."""
        schema = SCHEMA_DEFINITIONS.get(table_name, {})
        return list(schema.get("columns", {}).keys())