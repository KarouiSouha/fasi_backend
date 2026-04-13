"""
apps/ai_insights/services/langgraph_workflow.py
------------------------------------------------
CORRECTIONS v4 :
  - Fallback = "sql" (jamais "llm_only" par défaut)
  - SQL score ≥ 1 → toujours sql ou hybrid, jamais llm_only
  - Ajout mots-clés pour les nouveaux intents :
    branch_movement_cross, naming_explanation, customer_inactive_debt
  - Mots-clés arabes complets pour tous les domaines
"""

import re


class LangGraphWorkflow:

    SQL_KEYWORDS = [
        # Ventes
        "sales", "sold", "sell", "quantity", "turnover", "revenue", "product", "produit",
        "quantité", "chiffre", "total", "montant", "vente", "vendu", "volume",
        # Mois
        "january", "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
        "month", "year", "mois", "année", "période",
        # Entités
        "stock", "inventory", "inventaire", "client", "customer",
        "branch", "branche", "aging", "receivable", "overdue", "créance",
        "purchase", "achat", "transfer", "mouvement", "retour", "return",
        "compare", "comparer", "fournisseur", "supplier", "margin", "marge",
        "liste", "list", "top", "classement",
        # Croisements et qualité données
        "représentées", "figurent", "croiser", "matching", "cross-reference",
        "présentes dans", "absent", "manquant",
        # Arabes
        "مردود", "مردودات", "مبيعات", "إيراد", "عميل", "فرع", "مخزون",
        "كمية", "شراء", "نقل", "تسوية", "تالف", "أول المدة",
        "جرد", "مخزن", "رصيد", "ذمم", "مديونية", "مستحقات",
        "متأخرات", "عمر الديون", "اعمار", "فروع", "موردون",
        "مطابقة", "الفروع في", "حركات", "غائب",
    ]

    VECTOR_KEYWORDS = [
        "why", "pourquoi", "explain", "explication", "trend", "tendance",
        "cause", "raison", "reasons", "analysis", "analyse",
        "behavior", "comportement", "profile", "anomaly", "anomalie",
        "recommend", "recommandation", "suggestion", "advice", "conseil",
        "risk", "risque", "risques", "urgent", "priorité", "strategy",
        "stratégie", "plan", "improve", "améliorer", "inactive", "churn",
        "forecast", "prévision", "santé", "health", "dashboard", "overview",
        # Terminologie / nomenclature
        "signifie", "what does", "qu'est-ce que", "etymology", "origin",
        "terminology", "terminologie", "nom du fichier", "sбab التسمية",
        "لماذا", "تحليل", "توصية", "خطة", "استراتيجية", "مخاطر",
        "ما معنى", "سبب",
    ]

    LLM_ONLY_KEYWORDS = [
        "how can i improve without data", "conseil général",
        "best practices in general", "مشورة عامة",
        # Only VERY generic questions with no data signal at all
    ]

    def decide(self, question: str) -> dict:
        text = (question or "").lower()
        scores = {
            "sql":      sum(1 for k in self.SQL_KEYWORDS      if k in text),
            "vector":   sum(1 for k in self.VECTOR_KEYWORDS   if k in text),
            "llm_only": sum(1 for k in self.LLM_ONLY_KEYWORDS if k in text),
        }

        # SQL prioritaire dès score ≥ 1
        if scores["sql"] >= 1 and scores["vector"] >= 2:
            mode = "hybrid"
        elif scores["sql"] >= 1:
            mode = "sql"
        elif scores["vector"] >= 2:
            mode = "vector"
        elif scores["llm_only"] >= 1 and scores["sql"] == 0 and scores["vector"] == 0:
            mode = "llm_only"
        elif scores["vector"] >= 1:
            mode = "vector"
        else:
            mode = "sql"  # Fallback toujours sql

        return {
            "mode":   mode,
            "reason": f"sql={scores['sql']} vector={scores['vector']} llm_only={scores['llm_only']}",
            "scores": scores,
        }

    def should_use_sql(self, question: str) -> bool:
        return self.decide(question)["mode"] in ("sql", "hybrid")

    def should_use_vector(self, question: str) -> bool:
        return self.decide(question)["mode"] in ("vector", "hybrid")

    def should_use_llm_only(self, question: str) -> bool:
        return self.decide(question)["mode"] == "llm_only"