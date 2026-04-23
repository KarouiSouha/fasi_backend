"""
apps/ai_insights/services/intent_classifier.py
-----------------------------------------------
Classification intelligente des intents avec scoring pondéré.

Remplace la logique de 25 if/elif dans RetrievalService._detect_intent().

Architecture :
  - Chaque intent a des patterns (regex) pondérés
  - Le score final détermine l'intent dominant
  - La confiance est calculée (0.0 → 1.0)
  - La complexité est détectée séparément
  - Les entités sont extraites via QueryWeaverService

Avantages vs l'existant :
  - Extensible sans modifier le graph
  - Testable unitairement
  - Scores explicites (débogage facile)
  - Confiance utilisable pour le routing conditionnel
"""

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Intent Definitions ────────────────────────────────────────────────────────

@dataclass
class IntentPattern:
    """Définit un intent avec ses patterns de détection."""
    name:        str
    patterns:    list[str]  # regex patterns
    weight:      int        # poids de base (1-10)
    priority:    int        # ordre d'évaluation (plus bas = évalué en premier)
    needs_data:  bool = True  # nécessite des données DB ?
    description: str = ""


# Ordre d'évaluation : priority croissante (0 = évalué en premier)
INTENT_REGISTRY: list[IntentPattern] = [
    # ── Terminologie / nomenclature (priorité max) ─────────────────────────
    IntentPattern(
        name="naming_explanation",
        priority=0,
        weight=10,
        needs_data=False,
        patterns=[
            r"الدمم", r"اعمار[\s_]الدمم", r"أعمار[\s_]الذمم",
            r"dammam", r"etymology", r"que signifie\b", r"what does.*mean",
            r"explication du nom", r"سبب التسمية", r"ما معنى", r"شرح الاسم",
            r"pourquoi.*fichier.*s'appelle", r"why.*file.*called",
        ],
        description="Questions sur la terminologie comptable arabe",
    ),

    # ── Dashboard global / analytique ─────────────────────────────────────
    IntentPattern(
        name="analytical",
        priority=1,
        weight=9,
        patterns=[
            r"résumé\s+(global|général|complet|financier)",
            r"summary\b", r"overview\b", r"dashboard\b",
            r"tableau\s+de\s+bord", r"situation\s+(générale|globale|financière)",
            r"santé\s+financière", r"santé\s+de\s+l'entreprise",
            r"top\s+\d+\s+risques?", r"3\s+risques?\s+les\s+plus",
            r"analyse\s+(complète|globale|générale)",
            r"bilan\s+(complet|global)",
        ],
        description="Analyse globale multi-sources",
    ),

    # ── Croisement client inactif + dette ─────────────────────────────────
    IntentPattern(
        name="customer_inactive_debt",
        priority=2,
        weight=9,
        patterns=[
            r"clients?\s+(avec|ayant)\s+(dette|créance|solde|impayé)",
            r"solde\s+en\s+attente.*sans\s+(achat|transaction|commande)",
            r"aucune\s+transaction.*dette",
            r"n'achète\s+plus.*doit",
            r"عملاء\s+بدون\s+حركة",
            r"مديونية\s+بدون\s+(مبيعات|شراء)",
            r"stopped\s+buying.*debt",
            r"inactive.*overdue",
        ],
        description="Clients avec solde mais sans activité récente",
    ),

    # ── Créances / aging ──────────────────────────────────────────────────
    IntentPattern(
        name="aging",
        priority=3,
        weight=8,
        patterns=[
            r"créance", r"créances", r"aging\b", r"retard\s+de\s+paiement",
            r"impayé", r"impayés", r"recouvrement", r"encours\s+client",
            r"débiteur", r"receivable", r"overdue\b",
            r"dette\s+client", r"dso\b", r"days?\s+sales?\s+outstanding",
            r"ذمم", r"مديونية", r"مستحقات", r"تحصيل", r"متأخرات",
            r"عمر\s+الديون", r"اعمار\b", r"اعمار\s+الدمم",
            r"\d+\s+jours?\s+(de\s+retard|overdue)",
            r"bucket.*aging", r"aging.*bucket",
        ],
        description="Créances clients et vieillissement des impayés",
    ),

    # ── Inventaire / stock ────────────────────────────────────────────────
    IntentPattern(
        name="inventory",
        priority=4,
        weight=8,
        patterns=[
            r"stock\b", r"inventaire\b", r"rupture\s+de\s+stock",
            r"en\s+stock", r"niveau\s+de\s+stock", r"valorisation\s+stock",
            r"valeur\s+(du\s+)?stock", r"inventory\b",
            r"out\s+of\s+stock", r"on\s+hand", r"zero\s+stock",
            r"disponibilité", r"disponible\b",
            r"مخزون", r"جرد", r"رصيد\s+المخزن", r"نفاد", r"مخزن\b",
            r"SKU", r"sku\b",
        ],
        description="Niveaux de stock et inventaire",
    ),

    # ── Croisement branches ↔ mouvements ──────────────────────────────────
    IntentPattern(
        name="branch_movement_cross",
        priority=5,
        weight=8,
        patterns=[
            r"représentées?\s+dans\s+les\s+mouvements",
            r"présentes?\s+dans\s+les\s+mouvements",
            r"branches?\s+dans\s+les\s+mouvements",
            r"figur(ent|e)\s+dans", r"apparaiss(ent|e)\s+dans",
            r"croiser\s+les\s+branches",
            r"الفروع\s+في\s+الحركات", r"مطابقة\s+الفروع",
            r"branches?\s+(absentes?|manquantes?)",
            r"not\s+in\s+the\s+branch\s+file",
        ],
        description="Comparaison branches officielles vs mouvements",
    ),

    # ── Produits abîmés ───────────────────────────────────────────────────
    IntentPattern(
        name="damaged",
        priority=6,
        weight=8,
        patterns=[
            r"abîmé", r"abime\b", r"produit\s+perdu",
            r"pertes?\s+produit", r"damaged\b", r"lost\s+goods",
            r"تالف", r"تلف\b", r"خسارة\s+مخزون", r"ف\.تالف",
            r"produits?\s+abîmés?", r"marchandise\s+perdue",
        ],
        description="Produits perdus ou endommagés",
    ),

    # ── Retours clients ───────────────────────────────────────────────────
    IntentPattern(
        name="returns_sale",
        priority=7,
        weight=8,
        patterns=[
            r"retour\s+client", r"retours\s+clients",
            r"مردودات\s+بيع", r"مردود\s+بيع",
            r"returned?\s+by\s+customer", r"customer\s+returns?",
            r"retours?\s+(de\s+)?marchandise",
        ],
        description="Retours produits par les clients",
    ),

    # ── Retours fournisseurs ──────────────────────────────────────────────
    IntentPattern(
        name="returns_buy",
        priority=8,
        weight=8,
        patterns=[
            r"retour\s+fournisseur", r"retours?\s+fournisseurs?",
            r"مردود\s+شراء", r"مردودات\s+شراء",
            r"return\s+to\s+supplier", r"supplier\s+returns?",
        ],
        description="Retours produits aux fournisseurs",
    ),

    # ── Transferts inter-branches ─────────────────────────────────────────
    IntentPattern(
        name="transfers",
        priority=9,
        weight=7,
        patterns=[
            r"transfert\b", r"transfer\b", r"نقل\b",
            r"inter-branch", r"inter\s+branch", r"entre\s+branches?",
            r"moved\s+between", r"نقل\s+بين\s+الفروع",
        ],
        description="Transferts de stock entre branches",
    ),

    # ── Ajustements stock ─────────────────────────────────────────────────
    IntentPattern(
        name="adjustments",
        priority=10,
        weight=7,
        patterns=[
            r"ajustement", r"adjustment\b", r"تسوية\b",
            r"régularisation\s+stock", r"correction\s+stock",
            r"ف\s+تسوية", r"stock\s+adjustment",
        ],
        description="Ajustements de stock",
    ),

    # ── Stock ouverture ───────────────────────────────────────────────────
    IntentPattern(
        name="opening_stock",
        priority=11,
        weight=7,
        patterns=[
            r"début\s+de\s+période", r"opening\s+stock",
            r"stock\s+initial", r"أول\s+المدة", r"بداية\s+الفترة",
            r"ouverture\s+stock", r"ف\.أول\s+المدة",
        ],
        description="Stock de début de période",
    ),

    # ── Tous les mouvements ───────────────────────────────────────────────
    IntentPattern(
        name="all_movements",
        priority=12,
        weight=7,
        patterns=[
            r"tous\s+les\s+mouvements", r"all\s+movements",
            r"tous\s+les\s+types\s+de\s+mouvement",
            r"كل\s+الحركات", r"جميع\s+الحركات",
            r"types?\s+de\s+mouvement", r"different\s+types?\s+of\s+movement",
        ],
        description="Récapitulatif de tous les types de mouvement",
    ),

    # ── Marge brute ───────────────────────────────────────────────────────
    IntentPattern(
        name="margin",
        priority=13,
        weight=7,
        patterns=[
            r"marge\b", r"margin\b", r"profit\b", r"bénéfice\b",
            r"rentabilité", r"gross\s+margin", r"marge\s+brute",
            r"هامش\b", r"ربح\b", r"ربحية\b",
            r"marge\s+par\s+produit", r"margin\s+by\s+product",
        ],
        description="Calcul de la marge brute",
    ),

    # ── Top produits achetés ──────────────────────────────────────────────
    IntentPattern(
        name="top_purchased",
        priority=14,
        weight=7,
        patterns=[
            r"top\s+(produits?\s+)?acheté", r"plus\s+acheté",
            r"most\s+purchased", r"most\s+bought",
            r"أكثر\s+شراء", r"produit\s+le\s+plus\s+acheté",
            r"best\s+selling\s+supplier", r"fournisseur\s+principal",
        ],
        description="Produits les plus achetés",
    ),

    # ── Achats ────────────────────────────────────────────────────────────
    IntentPattern(
        name="purchases",
        priority=15,
        weight=7,
        patterns=[
            r"achat\b", r"achats\b", r"acheté\b",
            r"fournisseur\b", r"fournisseurs\b",
            r"purchase\b", r"purchases\b", r"bought\b", r"supplier\b",
            r"procurement\b", r"approvisionnement\b",
            r"شراء\b", r"ف\s+شراء", r"مورد\b", r"موردون\b",
        ],
        description="Achats et fournisseurs",
    ),

    # ── Comparaison branches ──────────────────────────────────────────────
    IntentPattern(
        name="branch_comparison",
        priority=16,
        weight=6,
        patterns=[
            r"compare(r)?\s+.*branch", r"comparaison.*branch",
            r"vs\s+.*branch", r"branch.*vs\s+",
            r"différence.*branch", r"between.*branch",
            r"branche.*meilleure", r"best\s+branch",
            r"مقارنة.*فرع", r"قارن.*فرع",
        ],
        description="Comparaison de performance entre branches",
    ),

    # ── Classement branches ───────────────────────────────────────────────
    IntentPattern(
        name="branch_ranking",
        priority=17,
        weight=6,
        patterns=[
            r"top\s+(branches?|succursales?)", r"best\s+branch",
            r"meilleure\s+branche", r"branche\s+la\s+plus\s+(performante|active)",
            r"all\s+branches", r"toutes\s+les\s+branches",
            r"by\s+branch", r"par\s+branche", r"classement.*branch",
            r"كل\s+الفروع", r"بالفروع", r"per\s+branch",
        ],
        description="Classement de toutes les branches",
    ),

    # ── Liste branches ────────────────────────────────────────────────────
    IntentPattern(
        name="branches",
        priority=18,
        weight=6,
        patterns=[
            r"liste\s+des\s+branches", r"liste\s+des\s+succursales",
            r"combien\s+de\s+branches", r"nos\s+branches",
            r"all\s+our\s+branches", r"branch\s+locations?",
            r"قائمة\s+الفروع", r"عدد\s+الفروع", r"كل\s+الفروع",
            r"adresse.*branche", r"téléphone.*branche",
            r"numéro.*branche",
        ],
        description="Liste et coordonnées des branches",
    ),

    # ── Classement clients ────────────────────────────────────────────────
    IntentPattern(
        name="customer_ranking",
        priority=19,
        weight=6,
        patterns=[
            r"top\s+\d*\s*clients?", r"meilleur(s)?\s+clients?",
            r"top\s+\d*\s*customers?", r"best\s+customer",
            r"classement\s+client", r"clients?\s+par\s+(chiffre|ca|revenu)",
            r"أفضل\s+عميل", r"أكثر\s+عميل.*شراء",
            r"premier(s)?\s+clients?\s+en\s+termes",
        ],
        description="Classement des clients par CA",
    ),

    # ── Questions clients (liste/stats) ───────────────────────────────────
    IntentPattern(
        name="customers",
        priority=20,
        weight=5,
        patterns=[
            r"combien\s+de\s+clients?", r"liste\s+des\s+clients?",
            r"clients?\s+actifs?", r"clients?\s+inactifs?",
            r"code\s+compte", r"compte\s+client",
            r"how\s+many\s+customers?", r"customer\s+list",
            r"عملاء\b", r"قائمة\s+العملاء", r"عدد\s+العملاء",
        ],
        description="Informations sur les clients",
    ),

    # ── Top produits vendus ───────────────────────────────────────────────
    IntentPattern(
        name="top_products",
        priority=21,
        weight=5,
        patterns=[
            r"top[\s\-]selling", r"best[\s\-]selling",
            r"most\s+sold", r"best\s+seller",
            r"top\s+\d*\s*produits?", r"meilleur(s)?\s+produits?",
            r"produit\s+le\s+plus\s+vendu", r"أكثر\s+مبيعاً",
            r"أفضل\s+منتج", r"classement\s+produit",
        ],
        description="Produits les plus vendus",
    ),

    # ── Évolution mensuelle ───────────────────────────────────────────────
    IntentPattern(
        name="monthly_sales",
        priority=22,
        weight=5,
        patterns=[
            r"évolution\b", r"evolution\b", r"mensuel\b", r"mensuelle\b",
            r"par\s+mois", r"month\s+by\s+month", r"monthly\b",
            r"mois\s+par\s+mois", r"tendance\s+mensuelle",
            r"شهري\b", r"شهر\s+بشهر", r"كل\s+شهر",
            r"trend\s+monthly", r"monthly\s+trend",
        ],
        description="Évolution des ventes par mois",
    ),

    # ── Par catégorie ─────────────────────────────────────────────────────
    IntentPattern(
        name="category_sales",
        priority=23,
        weight=5,
        patterns=[
            r"catégorie\b", r"catégories\b", r"category\b",
            r"par\s+catégorie", r"by\s+category",
            r"famille\s+produit", r"gamme\b",
            r"فئة\b", r"نوع\s+المنتج",
        ],
        description="Ventes par catégorie produit",
    ),

    # ── Ventes générales (fallback) ───────────────────────────────────────
    IntentPattern(
        name="sales",
        priority=24,
        weight=3,
        patterns=[
            r"vente\b", r"ventes\b", r"vendu\b", r"chiffre\s+d'affaires",
            r"ca\b", r"revenue\b", r"revenues\b", r"turnover\b",
            r"مبيعات\b", r"إيراد\b", r"مبيع\b",
            r"sell\b", r"sold\b", r"sales\b",
        ],
        description="Ventes générales",
    ),
]

# Index par nom pour accès rapide
INTENT_BY_NAME = {ip.name: ip for ip in INTENT_REGISTRY}


# ── Complexity Signals ────────────────────────────────────────────────────────

COMPLEXITY_PATTERNS = [
    # Conditions avec seuils numériques
    r"plus\s+de\s+\d+", r"moins\s+de\s+\d+",
    r"supérieur\s+(à|a)\s+\d+", r"inférieur\s+(à|a)\s+\d+",
    r">\s*\d+", r"<\s*\d+", r"≥\s*\d+", r"≤\s*\d+",

    # Combinaisons multi-critères
    r"\bet\b.+\bet\b", r"\band\b.+\band\b",
    r"mais\s+pas\b", r"but\s+not\b",
    r"\bsauf\b", r"except\b", r"excepté\b",
    r"\bni\b.+\bni\b",

    # Comparaisons temporelles
    r"mais\s+pas\s+en\s+\w+",
    r"n'ont?\s+pas\s+(commandé|acheté)\s+depuis",
    r"inactifs?\s+depuis\b",
    r"apparu\s+pour\s+la\s+première\s+fois",
    r"haven't\s+ordered\s+since",

    # Croisements de sources
    r"avec\b.+\bet\b.+\baussi\b",
    r"qui\s+ont\b.+\bmais\b.+\bpas\b",
    r"clients?\s+ayant\b.+\bet\b",

    # Calculs dérivés
    r"ratio\b", r"pourcentage\s+de\b", r"proportion\b", r"taux\s+de\b",
    r"en\s+pourcentage", r"as\s+a\s+percentage",

    # Arabes — conditions complexes
    r"أكثر\s+من\s+\d+", r"أقل\s+من\s+\d+",
    r"لكن\s+ليس\b", r"باستثناء\b",
    r"الذين\s+لم\b",
]


# ── IntentClassifier ──────────────────────────────────────────────────────────

class IntentClassifier:
    """
    Classifie l'intent d'une question avec scoring pondéré.
    
    Remplace le routing if/elif de RetrievalService._detect_intent()
    et le keyword scoring de LangGraphWorkflow.decide().
    """

    def classify(self, question: str, company=None) -> dict:
        """
        Classifie l'intent principal de la question.
        
        Returns:
            {
                "type":          str,            # intent dominant
                "confidence":    float,          # 0.0 → 1.0
                "complexity":    str,            # "simple" | "complex" | "ambiguous"
                "scores":        dict,           # scores par intent
                "branch_names":  list[str],
                "customer_name": str,
                "product_names": list[str],
                "supplier_name": str,
                "top_n":         int,
            }
        """
        text = (question or "").lower()

        # 1. Scorer chaque intent
        scores = self._score_intents(text)

        # 2. Choisir l'intent dominant
        best_intent, confidence = self._select_intent(scores)

        # 3. Évaluer la complexité
        complexity = self._evaluate_complexity(text)

        # 4. Extraire les entités
        entities = self._extract_entities(question, company)

        logger.debug(
            "[IntentClassifier] intent=%s confidence=%.2f complexity=%s "
            "scores=%s",
            best_intent, confidence, complexity,
            {k: v for k, v in scores.items() if v > 0}
        )

        return {
            "type":          best_intent,
            "confidence":    round(confidence, 2),
            "complexity":    complexity,
            "scores":        scores,
            **entities,
        }

    def _score_intents(self, text: str) -> dict[str, float]:
        """Calcule le score de chaque intent."""
        scores: dict[str, float] = {}

        for intent_def in sorted(INTENT_REGISTRY, key=lambda x: x.priority):
            score = 0.0
            for pattern in intent_def.patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                if matches:
                    # Plus de matchs = score plus élevé, mais avec diminution marginale
                    score += intent_def.weight * (1 + 0.3 * (len(matches) - 1))

            if score > 0:
                scores[intent_def.name] = round(score, 2)

        return scores

    def _select_intent(self, scores: dict[str, float]) -> tuple[str, float]:
        """Sélectionne l'intent dominant avec sa confiance."""
        if not scores:
            return "sales", 0.3

        # Intent avec le score maximum
        best_intent = max(scores, key=scores.get)
        max_score   = scores[best_intent]

        # Calculer la confiance relative
        # (écart entre le meilleur et le second meilleur)
        sorted_scores = sorted(scores.values(), reverse=True)
        if len(sorted_scores) >= 2:
            second_best = sorted_scores[1]
            gap_ratio   = (max_score - second_best) / max_score if max_score > 0 else 0
            # Confiance = combination du score absolu et de l'écart relatif
            base_conf   = min(1.0, max_score / 30.0)
            gap_conf    = min(1.0, gap_ratio * 2)
            confidence  = (base_conf * 0.6 + gap_conf * 0.4)
        else:
            confidence = min(1.0, max_score / 20.0)

        return best_intent, confidence

    def _evaluate_complexity(self, text: str) -> str:
        """Évalue la complexité de la question."""
        complexity_score = sum(
            1 for pattern in COMPLEXITY_PATTERNS
            if re.search(pattern, text, re.IGNORECASE)
        )

        if complexity_score >= 2:
            return "complex"
        elif complexity_score == 1:
            return "ambiguous"
        return "simple"

    def _extract_entities(self, question: str, company=None) -> dict:
        """Extrait les entités nommées de la question."""
        from apps.ai_insights.services.query_weaver_service import QueryWeaverService

        qw = QueryWeaverService()
        text = question.lower()

        result = {
            "branch_names":  [],
            "customer_name": "",
            "product_names": [],
            "supplier_name": "",
            "top_n":         self._extract_top_n(text),
            "date_range":    None,
        }

        # Extraire avec contexte company si disponible
        if company:
            try:
                result["branch_names"]  = qw.parse_branch_names(question, company)
                result["product_names"] = qw.parse_product_names(question, company)
            except Exception as exc:
                logger.debug("[IntentClassifier] Entity extraction DB failed: %s", exc)

        try:
            result["customer_name"] = qw.parse_customer_name(question, company)
            result["supplier_name"] = self._extract_supplier_name(text)
            start, end = qw.parse_date_range(question)
            if start and end:
                result["date_range"] = {"start": str(start), "end": str(end)}
        except Exception as exc:
            logger.debug("[IntentClassifier] Entity extraction failed: %s", exc)

        return result

    @staticmethod
    def _extract_top_n(text: str) -> int:
        """Extrait le nombre N dans des expressions comme 'top 5', 'les 10 premiers'."""
        patterns = [
            r"top[\s\-]?(\d+)",
            r"(\d+)\s+(?:best|top|selling|premier|first|meilleur|client|produit)",
            r"(?:les|les\s+)(\d+)\s+(?:premiers?|premières?)",
            r"(\d+)\s+(?:plus|most)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return min(50, max(1, int(m.group(1))))
        return 5

    @staticmethod
    def _extract_supplier_name(text: str) -> str:
        """Extrait le nom du fournisseur."""
        for kw in ["fournisseur", "supplier", "chez", "auprès de", "from", "مورد"]:
            pattern = rf"(?:{re.escape(kw)})\s+(?:named\s+|appelé\s+)?([A-Za-z0-9\u0600-\u06FF/\s\-\.]+?)(?:\?|,|\.|$)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                candidate = match.group(1).strip()[:80]
                if len(candidate) >= 2:
                    return candidate

        known = re.search(r"\b(ELAN|LINKNET|LEGRAND|OWER\s*GROUP|ASTON)\b", text.upper())
        if known:
            return known.group(1)
        return ""

    def get_intent_description(self, intent_name: str) -> str:
        """Retourne la description d'un intent."""
        ip = INTENT_BY_NAME.get(intent_name)
        return ip.description if ip else "Intent inconnu"

    def list_intents(self) -> list[str]:
        """Retourne la liste de tous les intents supportés."""
        return [ip.name for ip in INTENT_REGISTRY]