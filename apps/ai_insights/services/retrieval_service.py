"""
apps/ai_insights/services/retrieval_service.py
------------------------------------------------
CORRECTIONS v4 :
  FIX-1 : Tous les appels à sql_service utilisent les vrais noms de méthodes
  FIX-2 : Ordre de détection corrigé — damaged AVANT all_movements
  FIX-3 : Intent "customers" route vers get_customer_list + get_customers_stats
  FIX-4 : Intent "branches" route vers get_branches_list
  FIX-5 : Fallback date range utilise les vraies dates des données
  FIX-6 : analytical enrichit toutes les sections
  FIX-7 [NOUVEAU] : Intent "branch_movement_cross" — croisement branches officielles
          vs branches dans les mouvements (répond à la vraie question)
  FIX-8 [NOUVEAU] : Intent "naming_explanation" — explication terminologie comptable
          arabe (اعمار الديون ≠ ville de Dammam)
  FIX-9 [NOUVEAU] : Intent "customer_inactive_debt" — clients avec solde en attente
          mais sans transaction récente (croisement aging + mouvements)
"""

import logging
import datetime
import re

from django.conf import settings

from .langgraph_workflow import LangGraphWorkflow
from .query_weaver_service import QueryWeaverService
from .sql_service import SQLService
from .qdrant_service import QdrantService, QdrantServiceUnavailable
from .text_to_sql_service import TextToSQLService, TextToSQLError

logger = logging.getLogger(__name__)


class RetrievalService:
    def __init__(self):
        self.workflow     = LangGraphWorkflow()
        self.query_weaver = QueryWeaverService()
        self.sql          = SQLService()
        self.qdrant_service = None
        self.text_to_sql     = TextToSQLService()
        self._init_qdrant()

    def _init_qdrant(self):
        from django.conf import settings
        if not getattr(settings, "QDRANT_URL", "").strip():
            return
        try:
            self.qdrant_service = QdrantService()
        except QdrantServiceUnavailable as exc:
            logger.warning("[RetrievalService] Qdrant unavailable: %s", exc)

    # ══════════════════════════════════════════════════════════════════════════
    # POINT D'ENTREE PRINCIPAL
    # ══════════════════════════════════════════════════════════════════════════

    def build_context(self, question: str, company, companies: list = None) -> dict:
        if not companies:
            companies = [company] if company else []

        intent = self._detect_intent(question, company)
        start_date, end_date = self._resolve_dates(question, company, intent)
        start_date, end_date = self._validate_date_range(company, start_date, end_date, intent)

        logger.debug(
            "[RetrievalService] intent=%s branches=%s customer=%s products=%s dates=%s→%s",
            intent.get("type"), intent.get("branch_names"),
            intent.get("customer_name"), intent.get("product_names"),
            start_date, end_date,
        )

        t = intent["type"]

        # ── INVENTAIRE ────────────────────────────────────────────────────────
        if t == "inventory":
            branch_name  = intent.get("branch_names", [None])[0] if intent.get("branch_names") else None
            product_names = intent.get("product_names", [])
            product_name  = product_names[0] if product_names else ""
            inv = self.sql.get_inventory_summary(
                company,
                branch_name=branch_name,
                product_name=product_name or None,
            )
            return {
                "mode":          "inventory",
                "filter_branch": branch_name,
                "filter_product": product_name or None,
                **inv,
            }

        # ── CRÉANCES ──────────────────────────────────────────────────────────
        if t == "aging":
            customer_name = intent.get("customer_name", "")
            aging_data = self.sql.get_aging_summary(company, customer_name=customer_name or None)
            customers_stats = self.sql.get_customers_stats(company)
            return {
                "mode":            "aging",
                "filter_customer": customer_name or None,
                "customers_stats": customers_stats,
                **aging_data,
            }

        # ── ANALYTIQUE ────────────────────────────────────────────────────────
        if t == "analytical":
            return self._build_analytical(company, start_date, end_date)

        # ── CROISEMENT BRANCHES ↔ MOUVEMENTS [FIX-7 NOUVEAU] ─────────────────
        if t == "branch_movement_cross":
            cross = self.sql.get_branch_movement_cross(company)
            movements_summary = self.sql.get_all_movements_summary(company, start_date, end_date)
            return {
                "mode":              "branch_movement_cross",
                "cross":             cross,
                "movements_summary": movements_summary,
                "period":            f"{start_date} to {end_date}",
            }

        # ── EXPLICATION TERMINOLOGIE COMPTABLE [FIX-8 NOUVEAU] ───────────────
        if t == "naming_explanation":
            return {
                "mode": "naming_explanation",
                "term": intent.get("term", ""),
                "business_context": (
                    "Libyan technology distributor using Arabic ERP. "
                    "Files: العملاء___2026 (customers), اعمار__الدمم__2026 (aging receivables), "
                    "جرد__افقي__2026 (horizontal inventory), فروع_بروتكتا_2026 (branches), "
                    "حركة__المادة_2026 (material movements)."
                ),
            }

        # ── CROISEMENT CLIENT INACTIF + DETTE [FIX-9 NOUVEAU] ────────────────
        if t == "customer_inactive_debt":
            aging_data      = self.sql.get_aging_summary(company)
            customers_stats = self.sql.get_customers_stats(company)
            # Clients avec solde en aging mais 0 ventes sur la période
            no_sales_customers = self._find_debt_no_sales(company, start_date, end_date, aging_data)
            return {
                "mode":                  "customer_inactive_debt",
                "aging_data":            aging_data,
                "customers_stats":       customers_stats,
                "no_sales_customers":    no_sales_customers,
                "period":                f"{start_date} to {end_date}",
                "note": (
                    "Matching uses customer_name from movements vs account name from aging. "
                    "If naming conventions differ, some matches may be missed."
                ),
            }

        # ── COMPARAISON BRANCHES ──────────────────────────────────────────────
        if t == "branch_comparison":
            data = self.sql.get_sales_comparison_by_branches(
                company, intent["branch_names"], start_date, end_date)
            return {
                "mode":               "sql",
                "branch_comparison":  data,
                "period":             f"{start_date} to {end_date}",
                "branches_requested": intent["branch_names"],
            }

        # ── CLASSEMENT BRANCHES ───────────────────────────────────────────────
        if t == "branch_ranking":
            return {
                "mode":         "sql",
                "all_branches": self.sql.get_sales_summary_all_branches(
                    company, start_date, end_date, top_n=intent.get("top_n", 10)),
                "period":       f"{start_date} to {end_date}",
            }

        # ── DETAIL BRANCHE ────────────────────────────────────────────────────
        if t == "branch_detail":
            branch_name = intent["branch_names"][0] if intent.get("branch_names") else None
            return {
                "mode":            "sql",
                "branch_overview": self.sql.get_branch_full_overview(
                    company, branch_name, start_date, end_date),
            }

        # ── LISTE DES BRANCHES ────────────────────────────────────────────────
        if t == "branches":
            return {
                "mode":     "sql",
                "branches": self.sql.get_branches_list(company),
            }

        # ── VENTES PAR CLIENT ─────────────────────────────────────────────────
        if t == "customer_sales":
            cname = intent.get("customer_name", "")
            top_n = intent.get("top_n", 10)
            customer_sales = self.sql.get_sales_by_customer(
                company, start_date, end_date, customer_name=cname or None, top_n=top_n)
            customer_detail = None
            if cname:
                customer_detail = self.sql.get_customer_detail(
                    company, cname, start_date, end_date)

            if not customer_sales and not customer_detail:
                return {
                    "mode":    "sql",
                    "no_data": True,
                    "no_data_message": (
                        f"Aucune vente trouvée pour la période {start_date} → {end_date}. "
                        f"Données disponibles : {self._get_earliest(company)} → {self._get_latest(company)}."
                    ),
                    "period": f"{start_date} to {end_date}",
                }

            result = {
                "mode":            "sql",
                "customer_sales":  customer_sales,
                "period":          f"{start_date} to {end_date}",
                "top_n_requested": top_n,
            }
            if customer_detail:
                result["customer_detail"] = customer_detail
            return result

        # ── CLIENTS (liste / stats) ───────────────────────────────────────────
        if t == "customers":
            cname = intent.get("customer_name", "")
            top_n = max(1, min(intent.get("top_n", 5), 50))

            customer_list = self.sql.get_customer_list(
                company, search=cname or None, top_n=top_n)

            # If parsed customer_name is too broad/noisy, avoid empty context by
            # falling back to the first N customers.
            if cname and not customer_list:
                customer_list = self.sql.get_customer_list(
                    company, search=None, top_n=top_n)

            return {
                "mode":            "sql",
                "customers_stats": self.sql.get_customers_stats(company),
                "customer_list":   customer_list,
            }

        # ── VENTES PRODUIT ────────────────────────────────────────────────────
        if t == "product_sales":
            product_names = intent.get("product_names", [])
            if len(product_names) > 1:
                return {
                    "mode":               "sql",
                    "summary":            self.sql.get_sales_summary(company, start_date, end_date),
                    "product_sales_list": [
                        self.sql.get_product_detail(company, n, start_date, end_date)
                        for n in product_names
                    ],
                }
            pname = product_names[0] if product_names else intent.get("product_name", "")
            return {
                "mode":           "sql",
                "product_detail": self.sql.get_product_detail(company, pname, start_date, end_date),
            }

        # ── TOP PRODUITS ──────────────────────────────────────────────────────
        if t == "top_products":
            bn = intent.get("branch_names", [None])[0] if intent.get("branch_names") else None
            return {
                "mode":            "sql",
                "summary":         self.sql.get_sales_summary(company, start_date, end_date),
                "top_products":    self.sql.get_top_sold_products(
                    company, start_date, end_date,
                    top_n=intent.get("top_n", 10), branch_name=bn),
                "top_by_category": self.sql.get_sales_by_category(
                    company, start_date, end_date, top_n=5),
            }

        # ── VENTES MENSUELLES ─────────────────────────────────────────────────
        if t == "monthly_sales":
            bn = intent.get("branch_names", [None])[0] if intent.get("branch_names") else None
            return {
                "mode":          "sql",
                "monthly_sales": self.sql.get_monthly_sales(
                    company, start_date, end_date, branch_name=bn),
                "summary":       self.sql.get_sales_summary(company, start_date, end_date),
            }

        # ── CATÉGORIES ────────────────────────────────────────────────────────
        if t == "category_sales":
            return {
                "mode":           "sql",
                "category_sales": self.sql.get_sales_by_category(
                    company, start_date, end_date, top_n=15),
                "summary":        self.sql.get_sales_summary(company, start_date, end_date),
            }

        # ── ACHATS ────────────────────────────────────────────────────────────
        if t == "purchases":
            supplier = intent.get("supplier_name", "")
            purch    = self.sql.get_purchases_summary(company, start_date, end_date)
            suppliers = self.sql.get_purchases_by_supplier(
                company, start_date, end_date,
                supplier_name=supplier or None, top_n=10)
            result = {
                "mode":              "sql",
                "purchases_summary": purch,
                "top_suppliers":     suppliers,
            }
            if supplier:
                result["supplier_detail"] = self.sql.get_supplier_detail(
                    company, supplier, start_date, end_date)
            return result

        # ── TOP PRODUITS ACHETES ──────────────────────────────────────────────
        if t == "top_purchased":
            return {
                "mode":              "sql",
                "top_purchased":     self.sql.get_top_purchased_products(
                    company, start_date, end_date, top_n=intent.get("top_n", 10)),
                "purchases_summary": self.sql.get_purchases_summary(company, start_date, end_date),
            }

        # ── RETOURS CLIENTS ───────────────────────────────────────────────────
        if t == "returns_sale":
            return {
                "mode":         "sql",
                "returns_sale": self.sql.get_returns_sale_summary(company, start_date, end_date),
            }

        # ── RETOURS FOURNISSEURS ──────────────────────────────────────────────
        if t == "returns_buy":
            return {
                "mode":        "sql",
                "returns_buy": self.sql.get_returns_buy_summary(company, start_date, end_date),
            }

        # ── TRANSFERTS ────────────────────────────────────────────────────────
        if t == "transfers":
            return {
                "mode":      "sql",
                "transfers": self.sql.get_transfers_summary(company, start_date, end_date),
            }

        # ── PRODUITS ABIMÉS (ف.تالف) ──────────────────────────────────────────
        if t == "damaged":
            return {
                "mode":    "sql",
                "damaged": self.sql.get_damaged_summary(company, start_date, end_date),
            }

        # ── AJUSTEMENTS STOCK ────────────────────────────────────────────────
        if t == "adjustments":
            return {
                "mode":        "sql",
                "adjustments": self.sql.get_adjustments_summary(company, start_date, end_date),
            }

        # ── STOCK OUVERTURE ───────────────────────────────────────────────────
        if t == "opening_stock":
            product_name = intent.get("product_names", [None])[0] or ""
            return {
                "mode":          "sql",
                "opening_stock": self.sql.get_opening_stock_summary(
                    company, product_name=product_name or None),
            }

        # ── TOUS LES MOUVEMENTS ───────────────────────────────────────────────
        if t == "all_movements":
            return {
                "mode":          "sql",
                "all_movements": self.sql.get_all_movements_summary(
                    company, start_date, end_date),
            }

        # ── MARGE ─────────────────────────────────────────────────────────────
        if t == "margin":
            bn = intent.get("branch_names", [None])[0] if intent.get("branch_names") else None
            return {
                "mode":   "sql",
                "margin": self.sql.get_gross_margin(company, start_date, end_date, branch_name=bn),
            }

        # ── VECTOR ────────────────────────────────────────────────────────────
        if t == "vector":
            return self._build_vector(question, company)

        # ── HYBRID ────────────────────────────────────────────────────────────
        if t == "hybrid":
            return {
                "mode":           "hybrid",
                "sql_context":    self._build_global_sql(company, start_date, end_date),
                "vector_context": self._build_vector(question, company),
            }

        # ── LLM ONLY ─────────────────────────────────────────────────────────
        if t == "llm_only":
            return {
                "mode":             "llm_only",
                "business_summary": self._build_business_summary(company, start_date, end_date),
            }
            
        # Activé quand aucun des 25 intents ne correspond
        use_text_to_sql = getattr(settings, "AI_TEXT_TO_SQL_ENABLED", True)

        if use_text_to_sql:
            logger.info(
                "[RetrievalService] No intent matched — activating Text-to-SQL"
            )
            t2s_result = self.text_to_sql.query(
                question=question,
                company=company,
            )
            if t2s_result["success"]:
                return {
                    "mode":              "text_to_sql",
                    "text_to_sql":       t2s_result,
                    "prompt_context":    t2s_result["prompt_context"],
                    "display_data":      t2s_result["display_data"],
                    "sql_generated":     t2s_result["sql_generated"],
                    "explanation":       t2s_result["explanation"],
                    "confidence":        t2s_result["confidence"],
                }
            else:
                logger.warning(
                    "[RetrievalService] Text-to-SQL failed: %s",
                    t2s_result["error"],
                )

        # Dernier recours : résumé LLM-only
        return {
            "mode":             "llm_only",
            "business_summary": self._build_business_summary(
                company, start_date, end_date
            ),
        }

        # ── FALLBACK : résumé ventes ──────────────────────────────────────────
        return self._build_global_sql(company, start_date, end_date)

    # ══════════════════════════════════════════════════════════════════════════
    # DÉTECTION D'INTENT — ORDRE CRITIQUE
    # ══════════════════════════════════════════════════════════════════════════

    def _detect_intent(self, question: str, company=None) -> dict:
        t = question.lower()

        branch_names  = self.query_weaver.parse_branch_names(question, company)
        product_names = self.query_weaver.parse_product_names(question, company)
        customer_name = self.query_weaver.parse_customer_name(question, company)
        top_n         = self._extract_top_n(t)
        supplier_name = self._extract_supplier_name(t)
        product_name  = product_names[0] if product_names else self.query_weaver.parse_product_name(question, company)

        base = {
            "branch_names":  branch_names,
            "product_names": product_names,
            "product_name":  product_name,
            "customer_name": customer_name,
            "supplier_name": supplier_name,
            "top_n":         top_n,
        }

        # ORDRE : du plus spécifique au plus général

        # 0. Lookup direct client/code compte (doit passer avant nomenclature/aging)
        if self._is_customer_lookup_query(t):
            return {**base, "type": "customers"}

        # 1. Explication de nomenclature comptable [FIX-8]
        if self._is_naming_explanation(t):
            term = self._extract_naming_term(t)
            return {**base, "type": "naming_explanation", "term": term}

        # 2. Analytique global / dashboard
        if self._is_analytical(t):
            return {**base, "type": "analytical"}

        # 3. Créances / aging
        if self._is_aging(t):
            # Sous-cas : croisement client inactif + dette [FIX-9]
            if self._is_customer_inactive_debt(t):
                return {**base, "type": "customer_inactive_debt"}
            return {**base, "type": "aging"}

        # 3. Inventaire / stock
        if self._is_inventory(t):
            return {**base, "type": "inventory"}

        # 4. Produits abîmés — AVANT all_movements
        if self._is_damaged(t):
            return {**base, "type": "damaged"}

        # 5. Retours clients
        if self._is_return_sale(t):
            return {**base, "type": "returns_sale"}

        # 6. Retours fournisseurs
        if self._is_return_buy(t):
            return {**base, "type": "returns_buy"}

        # 7. Transferts
        if self._is_transfer(t):
            return {**base, "type": "transfers"}

        # 8. Ajustements
        if self._is_adjustment(t):
            return {**base, "type": "adjustments"}

        # 9. Stock ouverture
        if self._is_opening_stock(t):
            return {**base, "type": "opening_stock"}

        # 10. Tous les mouvements — APRÈS damaged
        if self._is_all_movements(t):
            return {**base, "type": "all_movements"}

        # 11. Croisement branches ↔ mouvements [FIX-7] — AVANT branches simple
        if self._is_branch_movement_cross(t):
            return {**base, "type": "branch_movement_cross"}

        # 12. Top produits achetés
        if self._is_top_purchased(t):
            return {**base, "type": "top_purchased"}

        # 13. Achats
        if self._is_purchase(t):
            return {**base, "type": "purchases"}

        # 14. Marge
        if self._is_margin(t):
            return {**base, "type": "margin"}

        # 15. Comparaison branches (≥2 branches + mot comparaison)
        if self.query_weaver.is_branch_comparison_question(t) and len(branch_names) >= 2:
            return {**base, "type": "branch_comparison"}

        # 16. Classement toutes branches
        if self.query_weaver.is_branch_ranking_question(t):
            return {**base, "type": "branch_ranking"}

        # 17. Liste des branches (téléphone, adresse, combien)
        if self._is_branches_list(t):
            return {**base, "type": "branches"}

        # 18. Détail branche (1 branche détectée)
        if branch_names and not product_names and not self._is_customer_question(t):
            return {**base, "type": "branch_detail"}

        # 19. Classement clients par CA
        if self._is_customer_ranking(t):
            return {**base, "type": "customer_sales"}

        # 20. Clients (lookup, stats, liste)
        if self._is_customer_question(t):
            return {**base, "type": "customers"}

        # 21. Produit spécifique
        if product_names or product_name:
            return {**base, "type": "product_sales"}

        # 22. Top produits vendus
        if self._is_top_products(t):
            return {**base, "type": "top_products"}

        # 23. Évolution mensuelle
        if self._is_monthly(t):
            return {**base, "type": "monthly_sales"}

        # 24. Par catégorie
        if self._is_category_sales(t):
            return {**base, "type": "category_sales"}

        # 25. Client avec nom
        if customer_name:
            return {**base, "type": "customer_sales"}

        # 26. Routing par mode LangGraph
        mode = self._get_base_mode(question)
        if mode in ("sql", "hybrid"):
            return {**base, "type": "sales"}
        if mode == "vector":
            return {**base, "type": "vector"}
        return {**base, "type": "llm_only"}
    
    

    # ══════════════════════════════════════════════════════════════════════════
    # DATES
    # ══════════════════════════════════════════════════════════════════════════

    def _resolve_dates(self, question: str, company=None, intent: dict = None):
        start, end = self.query_weaver.parse_date_range(question)
        if start and end:
            return start, end

        today = datetime.date.today()
        no_date_types = {"aging", "inventory", "customers", "branches", "opening_stock",
                         "branch_movement_cross", "naming_explanation", "customer_inactive_debt"}
        if (intent or {}).get("type") in no_date_types:
            return datetime.date(today.year, 1, 1), today

        return datetime.date(today.year, 1, 1), today

    def _validate_date_range(self, company, start_date, end_date, intent: dict):
        no_date_types = {"aging", "inventory", "customers", "branches", "opening_stock",
                         "branch_movement_cross", "naming_explanation", "customer_inactive_debt"}
        if (intent or {}).get("type") in no_date_types:
            return start_date, end_date
        if not start_date or not end_date:
            return self._get_earliest(company), self._get_latest(company)
        return start_date, end_date

    def _get_earliest(self, company):
        try:
            return self.sql.get_earliest_transaction_date(company)
        except Exception:
            return datetime.date(2026, 1, 1)

    def _get_latest(self, company):
        try:
            return self.sql.get_latest_transaction_date(company)
        except Exception:
            return datetime.date.today()

    # ══════════════════════════════════════════════════════════════════════════
    # CONSTRUCTION DES CONTEXTES
    # ══════════════════════════════════════════════════════════════════════════

    def _build_analytical(self, company, start_date, end_date) -> dict:
        sections = {}

        try:
            summary  = self.sql.get_sales_summary(company, start_date, end_date)
            monthly  = self.sql.get_monthly_sales(company, start_date, end_date)
            top_cust = self.sql.get_sales_by_customer(company, start_date, end_date, top_n=5)
            top_br   = self.sql.get_sales_summary_all_branches(company, start_date, end_date, top_n=5)
            sections["sales"] = {
                "summary":        summary,
                "monthly":        monthly,
                "top_customers":  top_cust,
                "top_branches":   top_br,
            }
        except Exception as e:
            logger.warning("[RetrievalService] analytical sales: %s", e)

        try:
            sections["purchases"] = {
                "summary":       self.sql.get_purchases_summary(company, start_date, end_date),
                "top_suppliers": self.sql.get_purchases_by_supplier(
                    company, start_date, end_date, top_n=5),
            }
        except Exception as e:
            logger.warning("[RetrievalService] analytical purchases: %s", e)

        try:
            sections["margin"] = self.sql.get_gross_margin(company, start_date, end_date)
        except Exception as e:
            logger.warning("[RetrievalService] analytical margin: %s", e)

        try:
            sections["aging"] = self.sql.get_aging_summary(company)
        except Exception as e:
            logger.warning("[RetrievalService] analytical aging: %s", e)

        try:
            sections["inventory"] = self.sql.get_inventory_summary(company)
        except Exception as e:
            logger.warning("[RetrievalService] analytical inventory: %s", e)

        try:
            sections["returns"] = {
                "sale": self.sql.get_returns_sale_summary(company, start_date, end_date),
                "buy":  self.sql.get_returns_buy_summary(company, start_date, end_date),
            }
        except Exception as e:
            logger.warning("[RetrievalService] analytical returns: %s", e)

        try:
            sections["customers"] = self.sql.get_customers_stats(company)
        except Exception as e:
            logger.warning("[RetrievalService] analytical customers: %s", e)

        try:
            sections["damaged"] = self.sql.get_damaged_summary(company, start_date, end_date)
        except Exception as e:
            logger.warning("[RetrievalService] analytical damaged: %s", e)

        return {
            "mode":     "analytical",
            "sections": sections,
            "period":   f"{start_date} to {end_date}",
        }

    def _build_global_sql(self, company, start_date, end_date) -> dict:
        summary = self.sql.get_sales_summary(company, start_date, end_date)
        return {
            "mode":    "sql",
            "summary": summary,
            "period":  f"{start_date} to {end_date}",
        }

    def _build_business_summary(self, company, start_date, end_date) -> str:
        try:
            s = self.sql.get_sales_summary(company, start_date, end_date)
            c = self.sql.get_customers_stats(company)
            return (
                f"Résumé: CA={s.get('total_revenue', 0):,.2f} LYD | "
                f"Transactions={s.get('transactions', 0)} | "
                f"Clients actifs={c.get('active', 0)} | "
                f"Période={start_date}→{end_date}"
            )
        except Exception:
            return "No business data available."

    def _build_vector(self, question: str, company) -> dict:
        if not self.qdrant_service:
            return {"mode": "vector", "items": []}
        try:
            from .openai_service import OpenAIService
            openai = OpenAIService()
            embedding  = openai.embed_texts([question])[0]
            company_id = str(company.id) if company else None
            results    = self.qdrant_service.search(embedding, company_id=company_id, top=6)
            items = [
                {
                    "score":    r.score,
                    "text":     (r.payload or {}).get("text", ""),
                    "metadata": r.payload or {},
                }
                for r in results
            ]
            return {"mode": "vector", "items": items}
        except Exception as e:
            logger.warning("[RetrievalService] vector search error: %s", e)
            return {"mode": "vector", "items": []}

    def _find_debt_no_sales(self, company, start_date, end_date, aging_data: dict) -> list:
        """
        FIX-9 : Identifie les clients avec solde aging > 0 mais aucune vente
        sur la période. Retourne une liste de dicts.
        Limitation : le matching se fait sur le nom (approximatif).
        """
        top_accounts = aging_data.get("top_accounts", [])
        if not top_accounts:
            return []

        results = []
        for acc in top_accounts:
            account_name = acc.get("account", "")
            if not account_name:
                continue
            # Chercher des ventes pour ce nom de compte
            sales = self.sql.get_sales_by_customer(
                company, start_date, end_date,
                customer_name=account_name, top_n=1
            )
            total_debt = acc.get("total", 0)
            if total_debt > 0:
                results.append({
                    "account":     account_name,
                    "total_debt":  total_debt,
                    "has_sales":   len(sales) > 0,
                    "sales_value": sales[0].get("total_revenue", 0) if sales else 0,
                })

        # Trier : d'abord ceux sans ventes, puis par montant de dette
        results.sort(key=lambda x: (x["has_sales"], -x["total_debt"]))
        return results

    # ══════════════════════════════════════════════════════════════════════════
    # DÉTECTEURS
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_naming_explanation(t: str) -> bool:
        """
        FIX-8 : Détecte les questions sur la nomenclature des fichiers ou termes comptables.
        Ex: "Pourquoi اعمار_الدمم ? Dammam en Libye ?"
        """
        # Do not route customer/account lookups to terminology explanation.
        customer_lookup_kw = [
            "account code", "code compte", "compte client", "customer account",
            "رقم الحساب", "رمز الحساب", "account_code", "accountcode",
        ]
        if any(k in t for k in customer_lookup_kw):
            return False

        naming_kw = [
            "nom du fichier", "pourquoi le nom", "que signifie",
            "what does", "why is it called", "explication du nom",
            "اعمار الدمم", "سبب التسمية", "ما معنى", "شرح الاسم",
            "dammam", "signification", "etymology",
        ]
        file_kw = [
            "اعمار", "fichier", "file", "الملف", "ملف",
        ]
        has_naming = any(k in t for k in naming_kw)
        has_file   = any(k in t for k in file_kw)
        # Au moins un mot de naming ET un contexte fichier/terme
        return has_naming or (has_file and ("?" in t or "pourquoi" in t or "لماذا" in t))

    @staticmethod
    def _extract_naming_term(t: str) -> str:
        """Extrait le terme sur lequel porte la question de nomenclature."""
        for term in ["اعمار_الدمم", "اعمار الدمم", "الدمم", "dammam", "اعمار"]:
            if term in t:
                return term
        return ""

    @staticmethod
    def _is_customer_inactive_debt(t: str) -> bool:
        """
        FIX-9 : Clients avec solde en aging mais sans transaction récente.
        """
        return any(k in t for k in [
            "solde en attente", "aucune transaction", "sans achat",
            "no transaction", "inactive", "inactif", "inactifs",
            "n'achète plus", "ne commande plus", "stopped buying",
            "عملاء بدون حركة", "رصيد بدون مبيعات", "مديونية بدون شراء",
        ]) and any(k in t for k in [
            "dette", "créance", "solde", "impayé", "ذمم", "مديونية",
            "aging", "اعمار",
        ])

    @staticmethod
    def _is_branch_movement_cross(t: str) -> bool:
        """
        FIX-7 : Croisement branches officielles ↔ branches dans les mouvements.
        """
        cross_kw = [
            "représentées dans les mouvements", "présentes dans les mouvements",
            "toutes représentées", "branches dans les mouvements",
            "mouvements qui ne figurent pas", "not in the branch file",
            "croiser les branches", "cross branches",
            "الفروع في الحركات", "فروع في حركة", "مطابقة الفروع",
            "figurent dans", "apparaissent dans",
        ]
        # Pattern : "branches" ET "mouvements" ET verbe de présence/absence
        has_cross = any(k in t for k in cross_kw)
        has_both  = (
            ("branch" in t or "فرع" in t or "مخزن" in t) and
            ("mouvement" in t or "حركة" in t) and
            any(k in t for k in ["figur", "représent", "présent", "absent", "manqu", "toutes"])
        )
        return has_cross or has_both

    @staticmethod
    def _is_analytical(t):
        import re
        patterns = [
            r"(résumé|summary|overview|rapport|dashboard|tableau\s+de\s+bord)",
            r"(santé|health|situation\s+(générale|globale|financière))",
            r"(risques?\s+(les\s+plus\s+urgent|priorit)|3\s+risques?|top\s+risques?)",
            r"(analyse\s+(complète|globale|générale|financière))",
        ]
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def _is_aging(t):
        return any(k in t for k in [
            "creance", "créance", "aging", "retard", "impayé", "impaye",
            "recouvrement", "echeance", "échéance", "encours",
            "débiteur", "debiteur", "receivable", "overdue", "dette",
            "90 jour", "60 jour", "risqué", "risque",
            "ذمم", "مديونية", "مستحقات", "تحصيل", "متأخرات",
            "عمر الديون", "دين", "اعمار", "dso",
        ])

    @staticmethod
    def _is_inventory(t):
        return any(k in t for k in [
            "stock", "inventaire", "rupture", "disponible", "en stock",
            "niveau de stock", "valorisation", "valeur stock", "inventory",
            "out of stock", "on hand", "zero stock", "zéro stock",
            "مخزون", "جرد", "رصيد المخزن", "نفاد", "مخزن",
        ])

    @staticmethod
    def _is_damaged(t):
        return any(k in t for k in [
            "abimé", "abime", "perdu", "perte", "damaged", "lost",
            "تالف", "تلف", "خسارة", "ف.تالف", "produit abîm",
        ])

    @staticmethod
    def _is_return_sale(t):
        if any(k in t for k in [
            "retour client", "retours client", "مردودات بيع",
            "مردود بيع", "returned by customer",
        ]):
            return True
        return "retour" in t and "fournisseur" not in t and "شراء" not in t

    @staticmethod
    def _is_return_buy(t):
        if any(k in t for k in [
            "retour fournisseur", "مردود شراء", "مردودات شراء",
            "return purchase", "returned to supplier",
        ]):
            return True
        return "retour" in t and ("fournisseur" in t or "شراء" in t)

    @staticmethod
    def _is_transfer(t):
        return any(k in t for k in [
            "transfert", "transfer", "نقل", "inter-branche",
            "inter branche", "moved between", "entre branches",
        ])

    @staticmethod
    def _is_adjustment(t):
        return any(k in t for k in [
            "ajustement", "adjustment", "تسوية", "regularisation",
            "correction stock", "ف تسوية",
        ])

    @staticmethod
    def _is_opening_stock(t):
        return any(k in t for k in [
            "debut de periode", "début de période", "opening stock",
            "stock initial", "أول المدة", "بداية الفترة",
            "ouverture stock", "stock ouverture",
        ])

    @staticmethod
    def _is_all_movements(t):
        return any(k in t for k in [
            "tous les mouvements", "all movements", "tous les types",
            "types de mouvement", "كل الحركات", "جميع الحركات",
            "quels sont les différents types", "what types",
            "different types",
        ])

    @staticmethod
    def _is_purchase(t):
        return any(k in t for k in [
            "achat", "achats", "acheté", "fournisseur", "fournisseurs",
            "purchase", "purchases", "bought", "supplier",
            "procurement", "approvisionnement",
            "شراء", "ف شراء", "مورد", "موردون",
        ])

    @staticmethod
    def _is_top_purchased(t):
        return any(k in t for k in [
            "top acheté", "plus acheté", "most purchased",
            "most bought", "أكثر شراء", "produit le plus acheté",
        ])

    @staticmethod
    def _is_margin(t):
        return any(k in t for k in [
            "marge", "margin", "profit", "bénéfice", "benefice",
            "rentabilité", "rentabilite", "gross margin",
            "هامش", "ربح", "ربحية",
        ])

    @staticmethod
    def _is_customer_ranking(t):
        return any(k in t for k in [
            "top client", "top clients", "top customer", "top customers",
            "meilleur client", "meilleurs clients", "best customer",
            "classement client", "clients par chiffre", "clients par ca",
            "أفضل عميل", "أكثر عميل",
        ])

    @staticmethod
    def _is_customer_question(t):
        return any(k in t for k in [
            "client", "customer", "عميل", "عملاء",
            "compte client", "liste client", "clients actifs",
            "combien de clients", "code compte",
        ])

    @staticmethod
    def _is_customer_lookup_query(t: str) -> bool:
        """Détecte les questions lookup client/code compte (nom -> code)."""
        return any(k in t for k in [
            "account code", "account-code", "customer account",
            "code compte", "compte client", "account_code",
            "رقم الحساب", "رمز الحساب", "كود الحساب",
        ]) and any(k in t for k in [
            "client", "customer", "عميل", "عملاء", "compte", "account",
        ])

    @staticmethod
    def _is_branches_list(t):
        return any(k in t for k in [
            "liste des branches", "all branches", "toutes les branches",
            "combien de branches", "liste des succursales",
            "كل الفروع", "قائمة الفروع", "عدد الفروع",
            "numéro de téléphone", "adresse", "nos branches",
        ])

    @staticmethod
    def _is_top_products(t):
        return any(k in t for k in [
            "top-selling", "top selling", "best-selling", "best selling",
            "most sold", "best seller", "top product", "top produit",
            "meilleur produit", "produit le plus vendu",
            "أكثر مبيعا", "أفضل منتج",
        ])

    @staticmethod
    def _is_monthly(t):
        return any(k in t for k in [
            "évolution", "evolution", "mensuel", "mensuelle",
            "par mois", "month by month", "monthly", "mois par mois",
            "شهري", "شهر بشهر",
        ])

    @staticmethod
    def _is_category_sales(t):
        return any(k in t for k in [
            "catégorie", "categorie", "category", "par catégorie",
            "by category", "famille", "gamme", "فئة", "نوع المنتج",
        ])

    @staticmethod
    def _extract_top_n(t: str) -> int:
        m = re.search(r"top[\s\-]?(\d+)", t)
        if m:
            return min(20, max(1, int(m.group(1))))
        m = re.search(r"(\d+)\s+(?:best|top|selling|premier|first|meilleur|clients?|produits?)", t)
        if m:
            return min(20, max(1, int(m.group(1))))
        m = re.search(r"(?:les|les\s+)(\d+)\s+(?:premiers?|premières?)", t)
        if m:
            return min(20, max(1, int(m.group(1))))
        return 5

    @staticmethod
    def _extract_supplier_name(t: str) -> str:
        for kw in ["fournisseur", "supplier", "chez", "auprès de", "from", "مورد"]:
            pattern = rf"(?:{re.escape(kw)})\s+(?:named\s+|appelé\s+)?([A-Za-z0-9\u0600-\u06FF/\s\-\.]+?)(?:\?|,|\.|$)"
            match = re.search(pattern, t, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()[:80]
                if len(candidate) >= 2:
                    return candidate
        known = re.search(r"\b(ELAN|LINKNET|LEGRAND|OWER\s*GROUP|ASTON)\b", t.upper())
        if known:
            return known.group(1)
        return ""

    def _get_base_mode(self, question: str) -> str:
        if not self.qdrant_service:
            return "sql"
        return self.workflow.decide(question)["mode"]

    # ── Compatibilité ascendante ──────────────────────────────────────────────

    def get_query_mode(self, question: str) -> str:
        return self._get_base_mode(question)

    def build_sql_context(self, question: str, company) -> dict:
        sd, ed = self._resolve_dates(question, company)
        return self._build_global_sql(company, sd, ed)

    def build_vector_context(self, question: str, company) -> dict:
        return self._build_vector(question, company)

    def build_hybrid_context(self, question: str, company) -> dict:
        sd, ed = self._resolve_dates(question, company)
        return {
            "mode":           "hybrid",
            "sql_context":    self._build_global_sql(company, sd, ed),
            "vector_context": self._build_vector(question, company),
        }