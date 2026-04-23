"""
apps/ai_insights/analyzers/churn_base.py
-----------------------------------------
Feature engineering partagé entre ChurnPredictor et HighValueChurnDetector.

Élimine la duplication de code entre les deux modules (DRY principle).
Toute modification au feature engineering se fait ici uniquement.

Features calculées :
  - days_since_last_purchase : jours depuis le dernier achat
  - purchase_count_12m       : nombre d'achats sur 12 mois
  - avg_monthly_revenue      : revenu mensuel moyen
  - avg_order_value          : valeur moyenne d'une commande
  - revenue_trend            : ratio (3 derniers mois / 3 mois précédents)
  - aging_risk_score         : score de risque créances (low/medium/high/critical)
  - overdue_ratio            : % de créances en retard
  - overdue_lyd              : montant en retard (LYD)
  - total_receivable_lyd     : encours total (LYD)

Scoring rule-based (pondérations) :
  - Recency (jours inactif) : 40%
  - Revenue trend           : 25%
  - Credit risk (aging)     : 25%
  - Overdue ratio           : 10%
"""

import logging
from datetime import date, timedelta

from django.db.models import Count, Max, Sum, Q
from django.db.models.functions import TruncMonth

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

ANALYSIS_WINDOW_DAYS  = 365
MIN_PURCHASES         = 3      # Minimum d'achats pour être analysé
PRE_FILTER_LIMIT      = 200    # Max clients avant feature engineering

# Seuils de récence (inactivité)
RECENCY_CRITICAL_DAYS = 90
RECENCY_HIGH_DAYS     = 60
RECENCY_MEDIUM_DAYS   = 30

# Poids du scoring rule-based
WEIGHT_RECENCY = 0.40
WEIGHT_TREND   = 0.25
WEIGHT_CREDIT  = 0.25
WEIGHT_OVERDUE = 0.10


# ── ChurnFeatureEngine ────────────────────────────────────────────────────────

class ChurnFeatureEngine:
    """
    Moteur de calcul de features pour la prédiction de churn.
    
    Usage :
        engine = ChurnFeatureEngine()
        features = engine.compute_features(company)
        scored = [engine.rule_based_score(f) for f in features]
    """

    def compute_features(self, company) -> list[dict]:
        """
        Calcule les features pour tous les clients éligibles.
        
        Processus :
          1. Récupérer les top 200 clients par revenu (pré-filtre)
          2. Calculer les métriques comportementales (achat, tendance)
          3. Croiser avec les données de créances (aging)
          4. Retourner une liste de feature dicts
        
        Returns:
            list[dict] avec les clés documentées en en-tête
        """
        from apps.transactions.models import MaterialMovement
        from apps.aging.models import AgingReceivable, AgingSnapshot
        from apps.customers.models import Customer

        today       = date.today()
        period_from = today - timedelta(days=ANALYSIS_WINDOW_DAYS)

        logger.info(
            "[ChurnFeatureEngine] Starting for company=%s window=%d days",
            company.id, ANALYSIS_WINDOW_DAYS
        )

        # ── Étape 1 : Top 200 clients par revenu (pré-filtre scalabilité) ─────
        sales_per_customer = (
            MaterialMovement.objects
            .filter(
                company=company,
                movement_type="ف بيع",
                movement_date__gte=period_from,
            )
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .values("customer_name")
            .annotate(
                purchase_count=Count("id"),
                last_purchase=Max("movement_date"),
                total_revenue=Sum("total_out"),
            )
            .order_by("-total_revenue")[:PRE_FILTER_LIMIT]
        )

        # ── Étape 2 : Tendance mensuelle par client ───────────────────────────
        monthly_by_customer = (
            MaterialMovement.objects
            .filter(
                company=company,
                movement_type="ف بيع",
                movement_date__gte=period_from,
            )
            .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .annotate(month=TruncMonth("movement_date"))
            .values("customer_name", "month")
            .annotate(monthly_revenue=Sum("total_out"))
            .order_by("customer_name", "month")
        )

        monthly_map: dict[str, list[float]] = {}
        for row in monthly_by_customer:
            monthly_map.setdefault(row["customer_name"], []).append(
                float(row["monthly_revenue"] or 0)
            )

        # ── Étape 3 : Données de créances (dernier snapshot) ─────────────────
        latest_snap = (
            AgingSnapshot.objects
            .filter(company=company)
            .order_by("-uploaded_at")
            .first()
        )

        aging_by_account: dict[str, dict] = {}
        if latest_snap:
            for rec in AgingReceivable.objects.filter(snapshot=latest_snap):
                key = rec.account_code or rec.account
                if not key:
                    continue

                total  = float(rec.total or 0)
                overdue_total = 0.0

                # Calculer le montant en retard si overdue_total n'existe pas directement
                if hasattr(rec, "overdue_total"):
                    overdue_total = float(rec.overdue_total or 0)
                elif total > 0 and hasattr(rec, "current"):
                    overdue_total = max(0.0, total - float(rec.current or 0))

                aging_by_account[key] = {
                    "risk_score":    getattr(rec, "risk_score", "unknown") or "unknown",
                    "overdue_ratio": overdue_total / total if total > 0 else 0.0,
                    "overdue_lyd":   overdue_total,
                    "total_lyd":     total,
                }

        # ── Étape 4 : Référentiel clients (pour account_code) ─────────────────
        customer_by_name = {
            c.name: c
            for c in Customer.objects.filter(company=company)
        }

        # ── Étape 5 : Construction des features ───────────────────────────────
        features: list[dict] = []
        skipped = 0

        for row in sales_per_customer:
            # Filtrer les clients avec peu d'achats (pas assez de signal)
            if (row["purchase_count"] or 0) < MIN_PURCHASES:
                skipped += 1
                continue

            cname      = row["customer_name"]
            last_date  = row["last_purchase"]
            total_rev  = float(row["total_revenue"] or 0)
            n_purchases = row["purchase_count"] or 0

            days_since = (today - last_date).days if last_date else 999

            # Métriques comportementales
            monthly_vals    = monthly_map.get(cname, [])
            avg_monthly_rev = sum(monthly_vals) / len(monthly_vals) if monthly_vals else 0.0
            avg_order_val   = total_rev / n_purchases if n_purchases > 0 else 0.0
            trend           = self.compute_trend(monthly_vals)

            # Données créances
            customer_obj = customer_by_name.get(cname)
            account_code = customer_obj.account_code if customer_obj else ""
            aging        = aging_by_account.get(account_code, {})

            features.append({
                # Identification
                "customer_name":            cname,
                "customer_id":              str(customer_obj.id) if customer_obj else None,
                "account_code":             account_code,

                # Comportement achat
                "days_since_last_purchase": days_since,
                "purchase_count_12m":       n_purchases,
                "avg_monthly_revenue":      round(avg_monthly_rev, 2),
                "avg_order_value":          round(avg_order_val, 2),
                "revenue_trend":            trend,
                "monthly_vals":             monthly_vals,  # Gardé pour analyses avancées

                # Créances
                "aging_risk_score":         aging.get("risk_score", "unknown"),
                "overdue_ratio":            round(aging.get("overdue_ratio", 0.0), 4),
                "overdue_lyd":              round(aging.get("overdue_lyd", 0.0), 2),
                "total_receivable_lyd":     round(aging.get("total_lyd", 0.0), 2),
            })

        logger.info(
            "[ChurnFeatureEngine] Done: %d features computed, %d skipped (<%d purchases)",
            len(features), skipped, MIN_PURCHASES
        )

        return features

    @staticmethod
    def compute_trend(monthly_vals: list[float]) -> float:
        """
        Calcule le ratio de tendance revenue : derniers 3 mois vs 3 mois précédents.
        
        Retourne :
          1.0  → stable (ou insuffisamment de données)
          >1.0 → croissance
          <1.0 → déclin
          2.0  → croissance depuis 0 (cas limite)
        """
        if len(monthly_vals) < 6:
            return 1.0  # Pas assez de données = tendance neutre

        recent = sum(monthly_vals[-3:])
        prior  = sum(monthly_vals[-6:-3])

        if prior == 0:
            return 1.0 if recent == 0 else 2.0

        return round(recent / prior, 4)

    def rule_based_score(self, f: dict) -> dict:
        """
        Calcule le score de churn rule-based.
        
        Pondération :
          Recency  40% : jours depuis le dernier achat
          Trend    25% : déclin du revenu trimestriel
          Credit   25% : risque créances (aging risk score)
          Overdue  10% : proportion de créances en retard
        
        Returns:
            f enrichi avec churn_score (0.0–1.0) et pre_label (low/medium/high/critical)
        """
        score = 0.0

        days  = f["days_since_last_purchase"]
        trend = f["revenue_trend"]
        risk  = f["aging_risk_score"]
        over  = f["overdue_ratio"]

        # ── Recency (40%) ──────────────────────────────────────────────────────
        if days >= RECENCY_CRITICAL_DAYS:
            score += 0.40  # ≥90 jours : critique
        elif days >= RECENCY_HIGH_DAYS:
            score += 0.28  # ≥60 jours : élevé
        elif days >= RECENCY_MEDIUM_DAYS:
            score += 0.14  # ≥30 jours : modéré

        # ── Revenue Trend (25%) ────────────────────────────────────────────────
        if trend < 0.50:
            score += 0.25  # Chute >50% : critique
        elif trend < 0.70:
            score += 0.18  # Chute 30-50% : élevé
        elif trend < 0.85:
            score += 0.10  # Chute 15-30% : modéré
        elif trend < 0.95:
            score += 0.04  # Légère baisse : faible

        # ── Credit Risk (25%) ──────────────────────────────────────────────────
        credit_weights = {
            "critical": 0.25,
            "high":     0.18,
            "medium":   0.08,
            "low":      0.00,
            "unknown":  0.05,  # Inconnu = légèrement pénalisé
        }
        score += credit_weights.get(risk, 0.05)

        # ── Overdue Ratio (10%) ────────────────────────────────────────────────
        if over >= 0.75:
            score += 0.10
        elif over >= 0.50:
            score += 0.06
        elif over >= 0.25:
            score += 0.02

        # Plafonner à 1.0
        score = min(1.0, round(score, 4))

        # Labeliser
        if score >= 0.75:
            label = "critical"
        elif score >= 0.50:
            label = "high"
        elif score >= 0.25:
            label = "medium"
        else:
            label = "low"

        return {**f, "churn_score": score, "pre_label": label}

    @staticmethod
    def derive_aging_risk_from_ratio(overdue_ratio: float, total_receivable: float) -> str:
        """
        Dérive l'aging_risk_score depuis le overdue_ratio quand il est "unknown".
        
        Utilisé en post-processing dans _format_result().
        """
        if total_receivable == 0:
            return "low"
        if overdue_ratio >= 0.75:
            return "critical"
        if overdue_ratio >= 0.50:
            return "high"
        if overdue_ratio >= 0.20:
            return "medium"
        return "low"

    def compute_features_batch(self, company, customer_names: list[str]) -> list[dict]:
        """
        Version limitée : calcule les features uniquement pour une liste de clients.
        Utile pour HighValueChurnDetector qui filtre déjà par seuil de revenu.
        """
        all_features = self.compute_features(company)
        name_set     = set(customer_names)
        return [f for f in all_features if f["customer_name"] in name_set]