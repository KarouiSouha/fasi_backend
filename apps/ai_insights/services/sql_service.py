"""
apps/ai_insights/services/sql_service.py
-----------------------------------------
CORRECTIONS v4 :

FIX-1 : _filter_branch() — supporte FK (branch__name) + texte (branch_name)
FIX-2 : get_sales_summary_all_branches() — annote le nom effectif de branche
FIX-3 : get_branches_list() — inclut les branches via FK si branch_name vide
FIX-4 : Aliases de méthodes pour retrieval_service
FIX-5 : get_customers_stats() — retourne total + active + inactive
FIX-6 [NOUVEAU] : zero_stock_count utilise .values("product_code").distinct().count()
         au lieu de .count() — évite de compter les lignes (1 par branche)
         et donne le vrai nombre de SKUs uniques en rupture (≤ sku_count toujours).
FIX-7 [NOUVEAU] : get_branch_movement_cross() — croisement branches officielles
         vs branches présentes dans les mouvements.
FIX-8 [NOUVEAU] : by_branch zero_stock annotate utilise Count distinct.
"""

import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum, Count, Q, Min, Max, Avg, Value
from django.db.models.functions import Coalesce, NullIf

from apps.transactions.models import MaterialMovement

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# TYPES DE MOUVEMENTS
# ══════════════════════════════════════════════════════════════════════════════

SALES_TYPES       = ["ف بيع"]
PURCHASE_TYPES    = ["ف شراء"]
RETURN_SALE_TYPES = ["مردودات بيع"]
RETURN_BUY_TYPES  = ["مردود شراء"]
TRANSFER_TYPES    = ["نقل"]
ADJUST_TYPES      = ["ف تسوية المخ"]
DAMAGE_TYPES      = ["ف.تالف"]
OPENING_TYPES     = ["ف.أول المدة"]
MAIN_IN_TYPES     = ["ادخال رئيسي"]
MAIN_OUT_TYPES    = ["اخراج رئيسي"]
SAMPLE_TYPES      = ["ف.عينات"]


class SQLService:

    # ── Normalisation ─────────────────────────────────────────────────────────

    def _n(self, value):
        """Normalize Decimal/None → float."""
        if value is None:
            return 0.0
        return float(value) if isinstance(value, Decimal) else float(value)

    # ── Filtres génériques ────────────────────────────────────────────────────

    def _base_qs(self, company, start_date=None, end_date=None, movement_types=None):
        qs = MaterialMovement.objects.filter(company=company)
        if start_date:
            qs = qs.filter(movement_date__gte=start_date)
        if end_date:
            qs = qs.filter(movement_date__lte=end_date)
        if movement_types:
            qs = qs.filter(movement_type__in=movement_types)
        return qs

    def _filter_branch(self, qs, branch_name):
        """
        FIX-1 : MovementsParser stocke branch= (FK Branch), pas branch_name= (texte).
        On filtre sur les deux pour couvrir tous les cas.
        """
        if branch_name:
            qs = qs.filter(
                Q(branch__name__icontains=branch_name) |
                Q(branch_name__icontains=branch_name)
            )
        return qs

    def _effective_branch_annotation(self):
        """
        FIX-2 : Retourne l'expression d'annotation pour le nom effectif de branche.
        Priorité : branch_name (texte) si non vide, sinon branch__name (FK).
        """
        return Coalesce(
            NullIf("branch_name", Value("")),
            "branch__name",
            Value(""),
        )

    def _filter_product(self, qs, product_name):
        if product_name:
            qs = qs.filter(
                Q(material_name__icontains=product_name) |
                Q(material_code__icontains=product_name)
            )
        return qs

    def _filter_customer(self, qs, customer_name):
        if customer_name:
            qs = qs.filter(customer_name__icontains=customer_name)
        return qs

    # ══════════════════════════════════════════════════════════════════════════
    # DATES
    # ══════════════════════════════════════════════════════════════════════════

    def get_earliest_transaction_date(self, company) -> date:
        try:
            result = (
                MaterialMovement.objects
                .filter(company=company, movement_date__isnull=False)
                .aggregate(earliest=Min("movement_date"))
            )
            d = result.get("earliest")
            if d:
                return d if isinstance(d, date) else d.date()
        except Exception as e:
            logger.warning("[SQLService] get_earliest_transaction_date: %s", e)
        return date(2026, 1, 1)

    def get_latest_transaction_date(self, company) -> date:
        try:
            result = (
                MaterialMovement.objects
                .filter(company=company, movement_date__isnull=False)
                .aggregate(latest=Max("movement_date"))
            )
            d = result.get("latest")
            if d:
                return d if isinstance(d, date) else d.date()
        except Exception as e:
            logger.warning("[SQLService] get_latest_transaction_date: %s", e)
        return date.today()

    # ══════════════════════════════════════════════════════════════════════════
    # RÉSUMÉ GLOBAL DES MOUVEMENTS
    # ══════════════════════════════════════════════════════════════════════════

    def get_all_movements_summary(self, company, start_date: date, end_date: date) -> dict:
        result = {
            "start_date": start_date.isoformat(),
            "end_date":   end_date.isoformat(),
        }
        type_map = {
            "sales":           (SALES_TYPES,       "qty_out", "total_out"),
            "purchases":       (PURCHASE_TYPES,    "qty_in",  "total_in"),
            "returns_sale":    (RETURN_SALE_TYPES, "qty_in",  "total_in"),
            "returns_buy":     (RETURN_BUY_TYPES,  "qty_out", "total_out"),
            "transfers_in":    (TRANSFER_TYPES,    "qty_in",  "total_in"),
            "transfers_out":   (TRANSFER_TYPES,    "qty_out", "total_out"),
            "adjustments_in":  (ADJUST_TYPES,      "qty_in",  "total_in"),
            "adjustments_out": (ADJUST_TYPES,      "qty_out", "total_out"),
            "damaged":         (DAMAGE_TYPES,      "qty_out", "total_out"),
            "opening_stock":   (OPENING_TYPES,     "qty_in",  "total_in"),
            "main_in":         (MAIN_IN_TYPES,     "qty_in",  "total_in"),
            "main_out":        (MAIN_OUT_TYPES,    "qty_out", "total_out"),
            "samples":         (SAMPLE_TYPES,      "qty_out", "total_out"),
        }
        for key, (types, qty_field, val_field) in type_map.items():
            qs = self._base_qs(company, start_date, end_date, movement_types=types)
            agg = qs.aggregate(
                qty=Sum(qty_field),
                val=Sum(val_field),
                trans=Count("id"),
            )
            result[key] = {
                "qty":          self._n(agg.get("qty")),
                "value":        self._n(agg.get("val")),
                "transactions": agg.get("trans") or 0,
            }
        return result

    # ══════════════════════════════════════════════════════════════════════════
    # VENTES (ف بيع)
    # ══════════════════════════════════════════════════════════════════════════

    def get_sales_summary(self, company, start_date: date, end_date: date,
                          branch_name: str = None) -> dict:
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = self._filter_branch(qs, branch_name)
        agg = qs.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )
        total_rev = self._n(agg.get("total_revenue"))
        trans     = agg.get("transactions") or 0
        return {
            "start_date":    start_date.isoformat(),
            "end_date":      end_date.isoformat(),
            "branch_name":   branch_name or "Toutes les branches",
            "total_qty":     self._n(agg.get("total_qty")),
            "total_revenue": total_rev,
            "transactions":  trans,
            "avg_ticket":    round(total_rev / trans, 2) if trans else 0,
        }

    def get_sales_by_branch(self, company, start_date: date, end_date: date,
                            branch_name: str = None) -> dict:
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = self._filter_branch(qs, branch_name)
        agg = qs.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )
        top_products = list(
            qs.exclude(Q(material_name__isnull=True) | Q(material_name=""))
            .values("material_name")
            .annotate(total_qty=Sum("qty_out"), total_revenue=Sum("total_out"))
            .order_by("-total_revenue")[:5]
        )
        top_customers = list(
            qs.exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .values("customer_name")
            .annotate(total_revenue=Sum("total_out"), transactions=Count("id"))
            .order_by("-total_revenue")[:5]
        )
        return {
            "branch_name":   branch_name or "Toutes les branches",
            "start_date":    start_date.isoformat(),
            "end_date":      end_date.isoformat(),
            "total_qty":     self._n(agg.get("total_qty")),
            "total_revenue": self._n(agg.get("total_revenue")),
            "transactions":  agg.get("transactions") or 0,
            "top_products": [
                {"material_name": r["material_name"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_revenue": self._n(r["total_revenue"])}
                for r in top_products
            ],
            "top_customers": [
                {"customer_name": r["customer_name"],
                 "total_revenue": self._n(r["total_revenue"]),
                 "transactions": r["transactions"] or 0}
                for r in top_customers
            ],
        }

    def get_sales_comparison_by_branches(self, company, branch_names: list,
                                          start_date: date, end_date: date) -> list:
        results = [
            self.get_sales_by_branch(company, start_date, end_date, branch_name=name)
            for name in branch_names
        ]
        results.sort(key=lambda x: -x["total_revenue"])
        return results

    def get_sales_summary_all_branches(self, company, start_date: date,
                                        end_date: date, top_n: int = 10) -> list:
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = qs.annotate(effective_branch=self._effective_branch_annotation())
        qs = qs.exclude(effective_branch="")
        rows = (
            qs.values("effective_branch")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("-total_revenue")[:top_n]
        )
        return [
            {
                "branch_name":   r["effective_branch"] or "Unknown",
                "total_qty":     self._n(r["total_qty"]),
                "total_revenue": self._n(r["total_revenue"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    def get_monthly_sales(self, company, start_date: date, end_date: date,
                          branch_name: str = None) -> list:
        from django.db.models.functions import TruncMonth
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = self._filter_branch(qs, branch_name)
        rows = (
            qs.annotate(month=TruncMonth("movement_date"))
            .values("month")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("month")
        )
        return [
            {
                "month":         r["month"].strftime("%Y-%m") if r["month"] else "?",
                "total_qty":     self._n(r["total_qty"]),
                "total_revenue": self._n(r["total_revenue"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    def get_sales_by_category(self, company, start_date: date, end_date: date,
                               top_n: int = 10) -> list:
        rows = (
            self._base_qs(company, start_date, end_date, SALES_TYPES)
            .exclude(Q(category__isnull=True) | Q(category=""))
            .values("category")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("-total_revenue")[:top_n]
        )
        return [
            {
                "category":      r["category"],
                "total_qty":     self._n(r["total_qty"]),
                "total_revenue": self._n(r["total_revenue"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # CLIENTS
    # ══════════════════════════════════════════════════════════════════════════

    def get_sales_by_customer(self, company, start_date: date, end_date: date,
                               customer_name: str = None, customer_code: str = None,
                               top_n: int = 10, branch_name: str = None) -> list:
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = self._filter_customer(qs, customer_name)
        qs = self._filter_branch(qs, branch_name)
        if customer_code:
            qs = qs.filter(customer_name__icontains=customer_code)
        rows = (
            qs.exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .values("customer_name")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("-total_revenue")[:top_n]
        )
        return [
            {
                "customer_name": r["customer_name"] or "Unknown",
                "account_code":  "",
                "total_qty":     self._n(r["total_qty"]),
                "total_revenue": self._n(r["total_revenue"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    def get_customer_detail(self, company, customer_name: str,
                             start_date: date, end_date: date) -> dict:
        qs_v = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs_v = self._filter_customer(qs_v, customer_name)
        agg_v = qs_v.aggregate(qty=Sum("qty_out"), revenue=Sum("total_out"), trans=Count("id"))
        qs_r = self._base_qs(company, start_date, end_date, RETURN_SALE_TYPES)
        qs_r = self._filter_customer(qs_r, customer_name)
        agg_r = qs_r.aggregate(qty=Sum("qty_in"), value=Sum("total_in"), trans=Count("id"))
        top_products = list(
            qs_v.exclude(Q(material_name__isnull=True) | Q(material_name=""))
            .values("material_name")
            .annotate(total_revenue=Sum("total_out"), total_qty=Sum("qty_out"))
            .order_by("-total_revenue")[:5]
        )
        return {
            "customer_name": customer_name,
            "period": f"{start_date} to {end_date}",
            "sales": {
                "total_qty":     self._n(agg_v.get("qty")),
                "total_revenue": self._n(agg_v.get("revenue")),
                "transactions":  agg_v.get("trans") or 0,
            },
            "returns": {
                "total_qty":    self._n(agg_r.get("qty")),
                "total_value":  self._n(agg_r.get("value")),
                "transactions": agg_r.get("trans") or 0,
            },
            "top_products": [
                {"material_name": r["material_name"],
                 "total_revenue": self._n(r["total_revenue"]),
                 "total_qty": self._n(r["total_qty"])}
                for r in top_products
            ],
        }

    def get_customers_stats(self, company) -> dict:
        """FIX-5 : Stats clients depuis Customer model."""
        try:
            from apps.customers.models import Customer
            total  = Customer.objects.filter(company=company).count()
            active = Customer.objects.filter(company=company, is_active=True).count()
            return {"total": total, "active": active, "inactive": total - active}
        except Exception:
            return {"total": 0, "active": 0, "inactive": 0}

    def get_customer_list(self, company, search: str = None, top_n: int = 20) -> list:
        """FIX-4 : Méthode manquante — liste de clients avec recherche."""
        try:
            from apps.customers.models import Customer
            qs = Customer.objects.filter(company=company)
            if search:
                qs = qs.filter(
                    Q(name__icontains=search) |
                    Q(account_code__icontains=search) |
                    Q(phone__icontains=search)
                )
            return list(
                qs.values("name", "account_code", "phone", "address", "area_code", "is_active")[:top_n]
            )
        except Exception:
            return []

    # ══════════════════════════════════════════════════════════════════════════
    # PRODUITS
    # ══════════════════════════════════════════════════════════════════════════

    def get_top_sold_products(self, company, start_date: date, end_date: date,
                               top_n: int = 10, branch_name: str = None,
                               category: str = None) -> list:
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = self._filter_branch(qs, branch_name)
        qs = qs.exclude(Q(material_name__isnull=True) | Q(material_name=""))
        if category:
            qs = qs.filter(category__icontains=category)
        rows = (
            qs.values("material_name", "category")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("-total_revenue")[:top_n]
        )
        return [
            {
                "material_name": r["material_name"] or "Unknown",
                "category":      r["category"] or "",
                "total_qty":     self._n(r["total_qty"]),
                "total_revenue": self._n(r["total_revenue"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    def get_product_sales(self, company, product_name: str, start_date: date,
                          end_date: date, branch_name: str = None) -> dict:
        qs = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs = self._filter_product(qs, product_name)
        qs = self._filter_branch(qs, branch_name)
        agg = qs.aggregate(
            total_qty=Sum("qty_out"),
            total_revenue=Sum("total_out"),
            transactions=Count("id"),
        )
        qs_branch = qs.annotate(effective_branch=self._effective_branch_annotation())
        by_branch = list(
            qs_branch.exclude(effective_branch="")
            .values("effective_branch")
            .annotate(total_qty=Sum("qty_out"), total_revenue=Sum("total_out"))
            .order_by("-total_revenue")
        )
        by_month = self._monthly_breakdown(qs, "qty_out", "total_out")
        return {
            "product_name":  product_name or "all products",
            "start_date":    start_date.isoformat(),
            "end_date":      end_date.isoformat(),
            "total_qty":     self._n(agg.get("total_qty")),
            "total_revenue": self._n(agg.get("total_revenue")),
            "transactions":  agg.get("transactions") or 0,
            "by_branch": [
                {"branch_name": r["effective_branch"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_revenue": self._n(r["total_revenue"])}
                for r in by_branch
            ],
            "by_month": by_month,
        }

    def get_product_detail(self, company, product_name: str,
                            start_date: date, end_date: date) -> dict:
        qs_v = self._base_qs(company, start_date, end_date, SALES_TYPES)
        qs_v = self._filter_product(qs_v, product_name)
        agg_v = qs_v.aggregate(qty=Sum("qty_out"), revenue=Sum("total_out"),
                                trans=Count("id"), avg_price=Avg("price_out"))
        qs_a = self._base_qs(company, start_date, end_date, PURCHASE_TYPES)
        qs_a = self._filter_product(qs_a, product_name)
        agg_a = qs_a.aggregate(qty=Sum("qty_in"), cost=Sum("total_in"),
                                trans=Count("id"), avg_price=Avg("price_in"))
        qs_r = self._base_qs(company, start_date, end_date, RETURN_SALE_TYPES)
        qs_r = self._filter_product(qs_r, product_name)
        agg_r = qs_r.aggregate(qty=Sum("qty_in"), value=Sum("total_in"), trans=Count("id"))

        revenue = self._n(agg_v.get("revenue"))
        cost    = self._n(agg_a.get("cost"))

        qs_branch = qs_v.annotate(effective_branch=self._effective_branch_annotation())
        by_branch = list(
            qs_branch.exclude(effective_branch="")
            .values("effective_branch")
            .annotate(total_qty=Sum("qty_out"), total_revenue=Sum("total_out"))
            .order_by("-total_revenue")
        )
        by_month = self._monthly_breakdown(qs_v, "qty_out", "total_out")

        stock_info = {}
        try:
            from apps.inventory.models import InventorySnapshotLine
            from django.db.models.functions import Coalesce as FnCoalesce
            inv_qs = InventorySnapshotLine.objects.filter(company=company).filter(
                Q(product_name__icontains=product_name) |
                Q(product_code__icontains=product_name)
            )
            inv_agg = inv_qs.aggregate(
                total_qty=FnCoalesce(Sum("quantity"), Decimal("0")),
                total_value=FnCoalesce(Sum("line_value"), Decimal("0")),
            )
            stock_by_branch = list(
                inv_qs.values("branch_name", "quantity", "line_value")
                .order_by("-quantity")
            )
            stock_info = {
                "total_qty":   self._n(inv_agg["total_qty"]),
                "total_value": self._n(inv_agg["total_value"]),
                "by_branch":   [
                    {"branch_name": r["branch_name"],
                     "quantity": self._n(r["quantity"]),
                     "line_value": self._n(r["line_value"])}
                    for r in stock_by_branch
                ],
            }
        except Exception:
            pass

        return {
            "product_name": product_name,
            "period":       f"{start_date} to {end_date}",
            "sales": {
                "total_qty":     self._n(agg_v.get("qty")),
                "total_revenue": revenue,
                "transactions":  agg_v.get("trans") or 0,
                "avg_price":     self._n(agg_v.get("avg_price")),
            },
            "purchases": {
                "total_qty":    self._n(agg_a.get("qty")),
                "total_cost":   cost,
                "transactions": agg_a.get("trans") or 0,
            },
            "returns": {
                "total_qty":    self._n(agg_r.get("qty")),
                "total_value":  self._n(agg_r.get("value")),
                "transactions": agg_r.get("trans") or 0,
            },
            "gross_margin":     round(revenue - cost, 2),
            "gross_margin_pct": round((revenue - cost) / revenue * 100, 1) if revenue else 0,
            "by_branch": [
                {"branch_name": r["effective_branch"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_revenue": self._n(r["total_revenue"])}
                for r in by_branch
            ],
            "by_month": by_month,
            "current_stock": stock_info,
        }

    def get_products_list(self, company, start_date: date, end_date: date) -> list:
        rows = (
            self._base_qs(company, start_date, end_date, SALES_TYPES)
            .exclude(Q(material_name__isnull=True) | Q(material_name=""))
            .values("material_code", "material_name", "category")
            .annotate(
                total_qty=Sum("qty_out"),
                total_revenue=Sum("total_out"),
                transactions=Count("id"),
            )
            .order_by("-total_revenue")
        )
        return [
            {
                "material_code": r["material_code"] or "",
                "material_name": r["material_name"],
                "category":      r["category"] or "",
                "total_qty":     self._n(r["total_qty"]),
                "total_revenue": self._n(r["total_revenue"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # ACHATS (ف شراء)
    # ══════════════════════════════════════════════════════════════════════════

    def get_purchases_summary(self, company, start_date: date, end_date: date,
                               branch_name: str = None) -> dict:
        qs = self._base_qs(company, start_date, end_date, PURCHASE_TYPES)
        qs = self._filter_branch(qs, branch_name)
        agg = qs.aggregate(
            total_qty=Sum("qty_in"),
            total_value=Sum("total_in"),
            transactions=Count("id"),
        )
        return {
            "start_date":   start_date.isoformat(),
            "end_date":     end_date.isoformat(),
            "total_qty":    self._n(agg.get("total_qty")),
            "total_value":  self._n(agg.get("total_value")),
            "transactions": agg.get("transactions") or 0,
        }

    def get_purchases_by_supplier(self, company, start_date: date, end_date: date,
                                   supplier_name: str = None, top_n: int = 10,
                                   branch_name: str = None) -> list:
        qs = self._base_qs(company, start_date, end_date, PURCHASE_TYPES)
        qs = self._filter_branch(qs, branch_name)
        if supplier_name:
            qs = qs.filter(
                Q(customer_name__icontains=supplier_name) |
                Q(customer__name__icontains=supplier_name)
            )
        rows = (
            qs.exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .values("customer_name")
            .annotate(
                total_qty=Sum("qty_in"),
                total_value=Sum("total_in"),
                transactions=Count("id"),
            )
            .order_by("-total_value")[:top_n]
        )
        return [
            {
                "supplier_name": r["customer_name"] or "Unknown",
                "total_qty":     self._n(r["total_qty"]),
                "total_value":   self._n(r["total_value"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    def get_supplier_detail(self, company, supplier_name: str,
                             start_date: date, end_date: date) -> dict:
        qs_a = self._base_qs(company, start_date, end_date, PURCHASE_TYPES)
        qs_a = qs_a.filter(
            Q(customer_name__icontains=supplier_name) |
            Q(customer__name__icontains=supplier_name)
        )
        agg_a = qs_a.aggregate(qty=Sum("qty_in"), value=Sum("total_in"), trans=Count("id"))
        top_products = list(
            qs_a.exclude(Q(material_name__isnull=True) | Q(material_name=""))
            .values("material_name")
            .annotate(total_qty=Sum("qty_in"), total_value=Sum("total_in"))
            .order_by("-total_value")[:10]
        )
        by_month = self._monthly_breakdown(qs_a, "qty_in", "total_in")
        qs_r = self._base_qs(company, start_date, end_date, RETURN_BUY_TYPES)
        qs_r = qs_r.filter(
            Q(customer_name__icontains=supplier_name) |
            Q(customer__name__icontains=supplier_name)
        )
        agg_r = qs_r.aggregate(qty=Sum("qty_out"), value=Sum("total_out"), trans=Count("id"))
        return {
            "supplier_name": supplier_name,
            "period":        f"{start_date} to {end_date}",
            "purchases": {
                "total_qty":    self._n(agg_a.get("qty")),
                "total_value":  self._n(agg_a.get("value")),
                "transactions": agg_a.get("trans") or 0,
            },
            "returns": {
                "total_qty":    self._n(agg_r.get("qty")),
                "total_value":  self._n(agg_r.get("value")),
                "transactions": agg_r.get("trans") or 0,
            },
            "top_products": [
                {"material_name": r["material_name"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_value": self._n(r["total_value"])}
                for r in top_products
            ],
            "by_month": by_month,
        }

    def get_top_purchased_products(self, company, start_date: date, end_date: date,
                                    top_n: int = 10, branch_name: str = None) -> list:
        qs = self._base_qs(company, start_date, end_date, PURCHASE_TYPES)
        qs = self._filter_branch(qs, branch_name)
        qs = qs.exclude(Q(material_name__isnull=True) | Q(material_name=""))
        rows = (
            qs.values("material_name", "category")
            .annotate(
                total_qty=Sum("qty_in"),
                total_value=Sum("total_in"),
                transactions=Count("id"),
                avg_price=Avg("price_in"),
            )
            .order_by("-total_qty")[:top_n]
        )
        return [
            {
                "material_name": r["material_name"],
                "category":      r["category"] or "",
                "total_qty":     self._n(r["total_qty"]),
                "total_value":   self._n(r["total_value"]),
                "avg_price":     self._n(r["avg_price"]),
                "transactions":  r["transactions"] or 0,
            }
            for r in rows
        ]

    # ══════════════════════════════════════════════════════════════════════════
    # RETOURS CLIENTS (مردودات بيع)
    # ══════════════════════════════════════════════════════════════════════════

    def get_returns_sale_summary(self, company, start_date: date, end_date: date,
                                  branch_name: str = None) -> dict:
        qs = self._base_qs(company, start_date, end_date, RETURN_SALE_TYPES)
        qs = self._filter_branch(qs, branch_name)
        agg = qs.aggregate(
            total_qty=Sum("qty_in"),
            total_value=Sum("total_in"),
            transactions=Count("id"),
        )
        top_products = list(
            qs.exclude(Q(material_name__isnull=True) | Q(material_name=""))
            .values("material_name")
            .annotate(total_qty=Sum("qty_in"), total_value=Sum("total_in"))
            .order_by("-total_qty")[:5]
        )
        top_customers = list(
            qs.exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .values("customer_name")
            .annotate(total_qty=Sum("qty_in"), total_value=Sum("total_in"))
            .order_by("-total_value")[:5]
        )
        return {
            "start_date":   start_date.isoformat(),
            "end_date":     end_date.isoformat(),
            "total_qty":    self._n(agg.get("total_qty")),
            "total_value":  self._n(agg.get("total_value")),
            "transactions": agg.get("transactions") or 0,
            "top_returned_products": [
                {"material_name": r["material_name"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_value": self._n(r["total_value"])}
                for r in top_products
            ],
            "top_customers": [
                {"customer_name": r["customer_name"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_value": self._n(r["total_value"])}
                for r in top_customers
            ],
        }

    def get_returns_sale(self, *args, **kwargs):
        return self.get_returns_sale_summary(*args, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # RETOURS FOURNISSEURS (مردود شراء)
    # ══════════════════════════════════════════════════════════════════════════

    def get_returns_buy_summary(self, company, start_date: date, end_date: date) -> dict:
        qs = self._base_qs(company, start_date, end_date, RETURN_BUY_TYPES)
        agg = qs.aggregate(
            total_qty=Sum("qty_out"),
            total_value=Sum("total_out"),
            transactions=Count("id"),
        )
        top_suppliers = list(
            qs.exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
            .values("customer_name")
            .annotate(total_qty=Sum("qty_out"), total_value=Sum("total_out"))
            .order_by("-total_value")[:5]
        )
        return {
            "start_date":   start_date.isoformat(),
            "end_date":     end_date.isoformat(),
            "total_qty":    self._n(agg.get("total_qty")),
            "total_value":  self._n(agg.get("total_value")),
            "transactions": agg.get("transactions") or 0,
            "top_suppliers": [
                {"supplier_name": r["customer_name"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_value": self._n(r["total_value"])}
                for r in top_suppliers
            ],
        }

    def get_returns_buy(self, *args, **kwargs):
        return self.get_returns_buy_summary(*args, **kwargs)

    def get_returns_summary(self, company, start_date: date, end_date: date) -> dict:
        return self.get_returns_sale_summary(company, start_date, end_date)

    # ══════════════════════════════════════════════════════════════════════════
    # TRANSFERTS (نقل)
    # ══════════════════════════════════════════════════════════════════════════

    def get_transfers_summary(self, company, start_date: date, end_date: date,
                               branch_name: str = None) -> dict:
        qs = self._base_qs(company, start_date, end_date, TRANSFER_TYPES)
        if branch_name:
            qs = self._filter_branch(qs, branch_name)
        agg_in  = qs.filter(qty_in__isnull=False).aggregate(
            qty=Sum("qty_in"), val=Sum("total_in"), trans=Count("id")
        )
        agg_out = qs.filter(qty_out__isnull=False).aggregate(
            qty=Sum("qty_out"), val=Sum("total_out"), trans=Count("id")
        )
        return {
            "start_date":    start_date.isoformat(),
            "end_date":      end_date.isoformat(),
            "transfers_in":  {
                "qty": self._n(agg_in.get("qty")),
                "value": self._n(agg_in.get("val")),
                "transactions": agg_in.get("trans") or 0,
            },
            "transfers_out": {
                "qty": self._n(agg_out.get("qty")),
                "value": self._n(agg_out.get("val")),
                "transactions": agg_out.get("trans") or 0,
            },
        }

    def get_transfers(self, *args, **kwargs):
        return self.get_transfers_summary(*args, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # AJUSTEMENTS (ف تسوية المخ)
    # ══════════════════════════════════════════════════════════════════════════

    def get_adjustments_summary(self, company, start_date: date, end_date: date) -> dict:
        qs = self._base_qs(company, start_date, end_date, ADJUST_TYPES)
        agg_in  = qs.aggregate(qty_in=Sum("qty_in"), val_in=Sum("total_in"), trans=Count("id"))
        agg_out = qs.aggregate(qty_out=Sum("qty_out"), val_out=Sum("total_out"))
        return {
            "start_date":      start_date.isoformat(),
            "end_date":        end_date.isoformat(),
            "adjustments_in":  {
                "qty":   self._n(agg_in.get("qty_in")),
                "value": self._n(agg_in.get("val_in")),
            },
            "adjustments_out": {
                "qty":   self._n(agg_out.get("qty_out")),
                "value": self._n(agg_out.get("val_out")),
            },
            "transactions": agg_in.get("trans") or 0,
        }

    def get_adjustments(self, *args, **kwargs):
        return self.get_adjustments_summary(*args, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # PRODUITS ABIMÉS (ف.تالف)
    # ══════════════════════════════════════════════════════════════════════════

    def get_damaged_summary(self, company, start_date: date, end_date: date) -> dict:
        qs = self._base_qs(company, start_date, end_date, DAMAGE_TYPES)
        agg = qs.aggregate(
            total_qty=Sum("qty_out"),
            total_value=Sum("total_out"),
            transactions=Count("id"),
        )
        items = list(
            qs.exclude(Q(material_name__isnull=True) | Q(material_name=""))
            .annotate(effective_branch=self._effective_branch_annotation())
            .values("material_name", "effective_branch", "movement_date")
            .annotate(qty=Sum("qty_out"), value=Sum("total_out"))
            .order_by("-value")[:20]
        )
        return {
            "start_date":   start_date.isoformat(),
            "end_date":     end_date.isoformat(),
            "total_qty":    self._n(agg.get("total_qty")),
            "total_value":  self._n(agg.get("total_value")),
            "transactions": agg.get("transactions") or 0,
            "items": [
                {
                    "product": r["material_name"] or "",
                    "branch":  r["effective_branch"] or "",
                    "date":    str(r.get("movement_date", "")),
                    "qty":     self._n(r.get("qty")),
                    "value":   self._n(r.get("value")),
                }
                for r in items
            ],
        }

    def get_damaged(self, *args, **kwargs):
        return self.get_damaged_summary(*args, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # STOCK DÉBUT DE PÉRIODE (ف.أول المدة)
    # ══════════════════════════════════════════════════════════════════════════

    def get_opening_stock_summary(self, company, product_name: str = None) -> dict:
        qs = self._base_qs(company, movement_types=OPENING_TYPES)
        if product_name:
            qs = self._filter_product(qs, product_name)
        agg = qs.aggregate(
            total_qty=Sum("qty_in"),
            total_value=Sum("total_in"),
            transactions=Count("id"),
        )
        qs_branch = qs.annotate(effective_branch=self._effective_branch_annotation())
        by_branch = list(
            qs_branch.exclude(effective_branch="")
            .values("effective_branch")
            .annotate(total_qty=Sum("qty_in"), total_value=Sum("total_in"))
            .order_by("-total_value")
        )
        return {
            "filter_product": product_name,
            "total_qty":      self._n(agg.get("total_qty")),
            "total_value":    self._n(agg.get("total_value")),
            "transactions":   agg.get("transactions") or 0,
            "by_branch": [
                {"branch_name": r["effective_branch"],
                 "total_qty": self._n(r["total_qty"]),
                 "total_value": self._n(r["total_value"])}
                for r in by_branch
            ],
        }

    def get_opening_stock(self, *args, **kwargs):
        return self.get_opening_stock_summary(*args, **kwargs)

    # ══════════════════════════════════════════════════════════════════════════
    # ENTRÉES / SORTIES PRINCIPALES
    # ══════════════════════════════════════════════════════════════════════════

    def get_main_entries_summary(self, company, start_date: date, end_date: date) -> dict:
        qs_in  = self._base_qs(company, start_date, end_date, MAIN_IN_TYPES)
        qs_out = self._base_qs(company, start_date, end_date, MAIN_OUT_TYPES)
        agg_in  = qs_in.aggregate(qty=Sum("qty_in"), val=Sum("total_in"), trans=Count("id"))
        agg_out = qs_out.aggregate(qty=Sum("qty_out"), val=Sum("total_out"), trans=Count("id"))
        return {
            "start_date":  start_date.isoformat(),
            "end_date":    end_date.isoformat(),
            "entries_in":  {
                "qty": self._n(agg_in.get("qty")),
                "value": self._n(agg_in.get("val")),
                "transactions": agg_in.get("trans") or 0,
            },
            "entries_out": {
                "qty": self._n(agg_out.get("qty")),
                "value": self._n(agg_out.get("val")),
                "transactions": agg_out.get("trans") or 0,
            },
        }

    # ══════════════════════════════════════════════════════════════════════════
    # BRANCHES
    # ══════════════════════════════════════════════════════════════════════════

    def get_branches_list(self, company) -> list:
        """FIX-3 : Récupère les branches depuis Branch model, puis fallback mouvements."""
        try:
            from apps.branches.models import Branch
            branches = list(
                Branch.objects.filter(is_active=True)
                .values("name", "address", "phone")
            )
            if branches:
                return branches
        except Exception:
            pass

        qs = (
            MaterialMovement.objects
            .filter(company=company)
            .annotate(effective_branch=self._effective_branch_annotation())
            .exclude(effective_branch="")
            .values("effective_branch")
            .distinct()
        )
        return [{"name": r["effective_branch"], "address": "", "phone": ""} for r in qs]

    def get_branch_full_overview(self, company, branch_name: str,
                                  start_date: date, end_date: date) -> dict:
        sales        = self.get_sales_by_branch(company, start_date, end_date, branch_name)
        purchases    = self.get_purchases_summary(company, start_date, end_date, branch_name)
        returns_sale = self.get_returns_sale_summary(company, start_date, end_date, branch_name)
        inv_data     = self._get_branch_inventory(company, branch_name)
        return {
            "branch_name": branch_name,
            "period":      f"{start_date} to {end_date}",
            "sales":       sales,
            "purchases":   purchases,
            "returns":     returns_sale,
            "inventory":   inv_data,
        }

    def _get_branch_inventory(self, company, branch_name: str) -> dict:
        try:
            from apps.inventory.models import InventorySnapshotLine
            from django.db.models.functions import Coalesce as FnCoalesce
            qs = InventorySnapshotLine.objects.filter(
                company=company,
                branch_name__icontains=branch_name,
            )
            agg = qs.aggregate(
                total_qty=FnCoalesce(Sum("quantity"), Decimal("0")),
                total_value=FnCoalesce(Sum("line_value"), Decimal("0")),
                sku_count=Count("product_code", distinct=True),
            )
            # FIX-6 : compter les SKUs uniques en rupture, pas les lignes
            zero_stock_count = (
                qs.filter(quantity=0)
                .values("product_code")
                .distinct()
                .count()
            )
            return {
                "total_qty":        self._n(agg["total_qty"]),
                "total_value":      self._n(agg["total_value"]),
                "sku_count":        agg["sku_count"],
                "zero_stock_count": zero_stock_count,
            }
        except Exception:
            return {}

    # ══════════════════════════════════════════════════════════════════════════
    # INVENTAIRE (InventorySnapshotLine)
    # FIX-6 : zero_stock_count = distinct product_codes à qty=0 (pas nb de lignes)
    # FIX-8 : by_branch zero_stock = Count distinct product_code
    # ══════════════════════════════════════════════════════════════════════════

    def get_inventory_summary(self, company, branch_name: str = None,
                               product_name: str = None, category: str = None) -> dict:
        try:
            from apps.inventory.models import InventorySnapshotLine
            from django.db.models.functions import Coalesce as FnCoalesce

            qs = InventorySnapshotLine.objects.filter(company=company)
            if branch_name:
                qs = qs.filter(branch_name__icontains=branch_name)
            if product_name:
                qs = qs.filter(
                    Q(product_name__icontains=product_name) |
                    Q(product_code__icontains=product_name)
                )
            if category:
                qs = qs.filter(product_category__icontains=category)

            agg = qs.aggregate(
                total_value=FnCoalesce(Sum("line_value"), Decimal("0")),
                total_qty=FnCoalesce(Sum("quantity"), Decimal("0")),
                sku_count=Count("product_code", distinct=True),
            )

            # FIX-6 : nombre de SKUs UNIQUES en rupture (≤ sku_count, toujours)
            zero_stock_sku_count = (
                qs.filter(quantity=0)
                .values("product_code")
                .distinct()
                .count()
            )

            # FIX-8 : by_branch zero_stock = Count distinct product_code à qty=0
            by_branch_raw = list(
                qs.values("branch_name")
                .annotate(
                    total_value=Sum("line_value"),
                    total_qty=Sum("quantity"),
                    sku_count=Count("product_code", distinct=True),
                )
                .order_by("-total_value")
            )

            # Calcul zero_stock par branche avec distinct product_code
            by_branch = []
            for row in by_branch_raw:
                bname = row["branch_name"] or "?"
                zero_sk = (
                    qs.filter(branch_name=row["branch_name"], quantity=0)
                    .values("product_code")
                    .distinct()
                    .count()
                )
                sk_count = row["sku_count"] or 1
                by_branch.append({
                    "branch_name": bname,
                    "total_value": self._n(row["total_value"]),
                    "total_qty":   self._n(row["total_qty"]),
                    "sku_count":   row["sku_count"],
                    "zero_stock":  zero_sk,
                    "zero_pct":    round(zero_sk / sk_count * 100, 1),
                })

            top_by_value = list(
                qs.filter(line_value__gt=0)
                .values("product_code", "product_name", "branch_name")
                .annotate(total_qty=Sum("quantity"), total_value=Sum("line_value"))
                .order_by("-total_value")[:10]
            )

            out_of_stock = list(
                qs.filter(quantity=0)
                .values("product_code", "product_name", "branch_name")[:20]
            )

            top_by_qty = list(
                qs.filter(quantity__gt=0)
                .values("product_code", "product_name", "branch_name")
                .annotate(total_qty=Sum("quantity"), total_value=Sum("line_value"))
                .order_by("-total_qty")[:10]
            )

            total_lines = qs.count()
            sku_count   = agg["sku_count"]

            return {
                "filter_branch":  branch_name,
                "filter_product": product_name,
                "summary": {
                    "total_value":      self._n(agg["total_value"]),
                    "total_qty":        self._n(agg["total_qty"]),
                    "sku_count":        sku_count,
                    "total_lines":      total_lines,
                    "zero_stock_count": zero_stock_sku_count,   # SKUs uniques, pas lignes
                    "zero_stock_lines": qs.filter(quantity=0).count(),  # pour info
                    # Sanity : zero_stock_count ≤ sku_count toujours
                },
                "by_branch":    by_branch,
                "top_by_value": [
                    {
                        "product_code": r["product_code"],
                        "product_name": r["product_name"],
                        "branch_name":  r["branch_name"],
                        "total_qty":    self._n(r["total_qty"]),
                        "total_value":  self._n(r["total_value"]),
                    }
                    for r in top_by_value
                ],
                "top_by_qty": [
                    {
                        "product_code": r["product_code"],
                        "product_name": r["product_name"],
                        "branch_name":  r["branch_name"],
                        "total_qty":    self._n(r["total_qty"]),
                        "total_value":  self._n(r["total_value"]),
                    }
                    for r in top_by_qty
                ],
                "out_of_stock": [
                    {
                        "product_code": r["product_code"],
                        "product_name": r["product_name"],
                        "branch_name":  r["branch_name"],
                    }
                    for r in out_of_stock
                ],
            }
        except Exception as e:
            logger.error("[SQLService] get_inventory_summary: %s", e)
            return {"summary": None, "error": str(e)}

    # ══════════════════════════════════════════════════════════════════════════
    # CROISEMENT BRANCHES OFFICIELLES ↔ MOUVEMENTS [FIX-7 NOUVEAU]
    # ══════════════════════════════════════════════════════════════════════════

    def get_branch_movement_cross(self, company) -> dict:
        """
        FIX-7 : Compare les branches du fichier officiel (Branch model / فروع)
        avec les branches présentes dans les mouvements (حركة_المادة).
        Retourne : matched, in_official_not_movements, in_movements_not_official.
        """
        official_branches = self.get_branches_list(company)
        official_names = [b.get("name", "").strip() for b in official_branches if b.get("name")]

        # Branches dans les mouvements (FK + texte)
        movement_branches_set = set()
        try:
            qs = (
                MaterialMovement.objects.filter(company=company)
                .annotate(eff=self._effective_branch_annotation())
                .exclude(eff="")
                .values_list("eff", flat=True)
                .distinct()
            )
            movement_branches_set = {n.strip() for n in qs}
        except Exception as e:
            logger.warning("[SQLService] get_branch_movement_cross movements: %s", e)

        matched                  = []
        in_official_not_movement = []
        in_movement_not_official = list(movement_branches_set)

        for oname in official_names:
            olow = oname.lower()
            found = False
            for mname in movement_branches_set:
                mlow = mname.lower()
                if olow == mlow or olow in mlow or mlow in oname.lower():
                    found = True
                    if mname in in_movement_not_official:
                        in_movement_not_official.remove(mname)
                    break
            if found:
                matched.append(oname)
            else:
                in_official_not_movement.append(oname)

        return {
            "official_branches":         official_branches,
            "official_count":            len(official_names),
            "movement_branches":         sorted(movement_branches_set),
            "movement_count":            len(movement_branches_set),
            "matched":                   matched,
            "matched_count":             len(matched),
            "in_official_not_movement":  in_official_not_movement,
            "in_movement_not_official":  in_movement_not_official,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # MARGE BRUTE
    # ══════════════════════════════════════════════════════════════════════════

    def get_gross_margin(self, company, start_date: date, end_date: date,
                         branch_name: str = None) -> dict:
        qs_v  = self._filter_branch(
            self._base_qs(company, start_date, end_date, SALES_TYPES), branch_name)
        qs_a  = self._filter_branch(
            self._base_qs(company, start_date, end_date, PURCHASE_TYPES), branch_name)
        qs_rv = self._base_qs(company, start_date, end_date, RETURN_SALE_TYPES)
        qs_ra = self._base_qs(company, start_date, end_date, RETURN_BUY_TYPES)

        revenue = self._n(qs_v.aggregate(v=Sum("total_out"))["v"])
        cost    = self._n(qs_a.aggregate(v=Sum("total_in"))["v"])
        ret_rev = self._n(qs_rv.aggregate(v=Sum("total_in"))["v"])
        ret_cos = self._n(qs_ra.aggregate(v=Sum("total_out"))["v"])

        net_revenue  = revenue - ret_rev
        net_cost     = cost - ret_cos
        gross_margin = net_revenue - net_cost

        return {
            "start_date":       start_date.isoformat(),
            "end_date":         end_date.isoformat(),
            "revenue":          revenue,
            "cost":             cost,
            "returns_revenue":  ret_rev,
            "returns_cost":     ret_cos,
            "net_revenue":      net_revenue,
            "net_cost":         net_cost,
            "gross_margin":     gross_margin,
            "gross_margin_pct": round(gross_margin / net_revenue * 100, 1) if net_revenue else 0,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # CRÉANCES / AGING (AgingReceivable)
    # ══════════════════════════════════════════════════════════════════════════

    def get_aging_summary(self, company, customer_name: str = None) -> dict:
        try:
            from apps.aging.models import AgingReceivable, AgingSnapshot
            from django.db.models.functions import Coalesce as FnCoalesce
        except ImportError:
            return {"aging_summary": None, "message": "AgingReceivable model not found"}

        snap = AgingSnapshot.objects.filter(company=company).order_by("-uploaded_at").first()
        if not snap:
            return {
                "aging_summary": None,
                "message": "Aucun rapport de créances disponible.",
            }

        qs = AgingReceivable.objects.filter(snapshot=snap)
        if customer_name:
            qs = qs.filter(account__icontains=customer_name)

        model_fields = {f.name for f in AgingReceivable._meta.get_fields() if hasattr(f, "name")}

        def _safe_field(*candidates):
            for c in candidates:
                if c in model_fields:
                    return c
            return None

        FIELD_MAP = {
            "current":  _safe_field("current", "d_current", "not_due"),
            "d1_30":    _safe_field("d1_30", "d_1_30", "days_1_30"),
            "d31_60":   _safe_field("d31_60", "d_31_60"),
            "d61_90":   _safe_field("d61_90", "d_61_90"),
            "d91_120":  _safe_field("d91_120", "d_91_120"),
            "d121_150": _safe_field("d121_150", "d_121_150"),
            "d151_180": _safe_field("d151_180", "d_151_180"),
            "d181_210": _safe_field("d181_210", "d_181_210"),
            "d211_240": _safe_field("d211_240"),
            "d241_270": _safe_field("d241_270"),
            "d271_300": _safe_field("d271_300"),
            "d301_330": _safe_field("d301_330"),
            "over_330": _safe_field("over_330", "d_over_330"),
            "total":    _safe_field("total", "balance", "total_amount"),
        }

        missing = [k for k, v in FIELD_MAP.items() if v is None]
        if missing:
            logger.warning("[SQLService AGING] Fields NOT mapped: %s", missing)

        agg_kwargs = {"account_count": Count("id")}
        for key, fname in FIELD_MAP.items():
            if fname:
                agg_kwargs[key] = FnCoalesce(Sum(fname), Decimal("0"))
            else:
                agg_kwargs[key] = FnCoalesce(Sum("id") * 0, Decimal("0"))

        try:
            agg = qs.aggregate(**agg_kwargs)
        except Exception as e:
            logger.error("[SQLService] aging aggregate error: %s", e)
            agg = {"account_count": qs.count()}
            for key in FIELD_MAP:
                agg[key] = Decimal("0")
            for row in qs.iterator(chunk_size=200):
                for key, fname in FIELD_MAP.items():
                    if fname:
                        val = getattr(row, fname, None) or Decimal("0")
                        agg[key] = agg.get(key, Decimal("0")) + Decimal(str(val))

        def _f(k):
            v = agg.get(k, 0)
            return float(v) if v is not None else 0.0

        total    = _f("total")
        current  = _f("current")
        d1_30    = _f("d1_30")
        d31_60   = _f("d31_60")
        d61_90   = _f("d61_90")
        d91_120  = _f("d91_120")
        d121_150 = _f("d121_150")
        d151_180 = _f("d151_180")
        over_180 = sum(_f(k) for k in [
            "d181_210", "d211_240", "d241_270", "d271_300", "d301_330", "over_330"
        ])
        overdue = total - current

        if total == 0:
            total   = current + d1_30 + d31_60 + d61_90 + d91_120 + d121_150 + d151_180 + over_180
            overdue = total - current

        # Détecter le bug d'import : total > 0 mais tous les buckets = 0
        bucket_sum = d1_30 + d31_60 + d61_90 + d91_120 + d121_150 + d151_180 + over_180 + current
        has_import_bug = (total > 0 and bucket_sum == 0)

        # Champs mapping pour les comptes critiques
        over90_fields = [
            FIELD_MAP[k] for k in
            ["d91_120", "d121_150", "d151_180", "d181_210", "d211_240",
             "d241_270", "d271_300", "d301_330", "over_330"]
            if FIELD_MAP.get(k)
        ]
        total_field = FIELD_MAP.get("total")
        order_field = f"-{total_field}" if total_field else "-id"

        critical_list = []
        if not has_import_bug:
            try:
                for ar in qs.order_by(order_field)[:300]:
                    over90 = sum(float(getattr(ar, f, 0) or 0) for f in over90_fields)
                    if over90 > 0:
                        ar_total = float(getattr(ar, total_field, 0) or 0) if total_field else 0
                        critical_list.append({
                            "account":    ar.account,
                            "total":      ar_total,
                            "overdue_90": over90,
                            "risk_score": getattr(ar, "risk_score", "") or "",
                            "d1_30":      float(getattr(ar, FIELD_MAP.get("d1_30") or "id", 0) or 0) if FIELD_MAP.get("d1_30") else 0,
                            "d31_60":     float(getattr(ar, FIELD_MAP.get("d31_60") or "id", 0) or 0) if FIELD_MAP.get("d31_60") else 0,
                            "d61_90":     float(getattr(ar, FIELD_MAP.get("d61_90") or "id", 0) or 0) if FIELD_MAP.get("d61_90") else 0,
                            "d91_120":    float(getattr(ar, FIELD_MAP.get("d91_120") or "id", 0) or 0) if FIELD_MAP.get("d91_120") else 0,
                        })
            except Exception as e:
                logger.warning("[SQLService] critical_list error: %s", e)
            critical_list.sort(key=lambda x: -x["overdue_90"])

        top_accounts = []
        try:
            for ar in qs.order_by(order_field)[:10]:
                ar_total = float(getattr(ar, total_field, 0) or 0) if total_field else 0
                if ar_total > 0:
                    top_accounts.append({
                        "account": ar.account,
                        "total":   ar_total,
                        "current": float(getattr(ar, FIELD_MAP.get("current") or "id", 0) or 0) if FIELD_MAP.get("current") else 0,
                        "d1_30":   float(getattr(ar, FIELD_MAP.get("d1_30") or "id", 0) or 0) if FIELD_MAP.get("d1_30") else 0,
                        "d31_60":  float(getattr(ar, FIELD_MAP.get("d31_60") or "id", 0) or 0) if FIELD_MAP.get("d31_60") else 0,
                        "d61_90":  float(getattr(ar, FIELD_MAP.get("d61_90") or "id", 0) or 0) if FIELD_MAP.get("d61_90") else 0,
                        "d91_120": float(getattr(ar, FIELD_MAP.get("d91_120") or "id", 0) or 0) if FIELD_MAP.get("d91_120") else 0,
                    })
        except Exception as e:
            logger.warning("[SQLService] top_accounts error: %s", e)

        return {
            "report_date":     snap.report_date.isoformat() if snap.report_date else None,
            "filter_customer": customer_name or None,
            "has_import_bug":  has_import_bug,
            "aging_summary": {
                "total":         total,
                "current":       current,
                "d1_30":         d1_30,
                "d31_60":        d31_60,
                "d61_90":        d61_90,
                "d91_120":       d91_120,
                "d121_150":      d121_150,
                "d151_180":      d151_180,
                "over_180":      over_180,
                "overdue_total": overdue,
                "account_count": agg.get("account_count") or 0,
                "pct_overdue":   round(overdue / total * 100, 1) if total else 0,
            },
            "critical_accounts": critical_list[:15],
            "top_accounts":      top_accounts[:10],
        }

    # ══════════════════════════════════════════════════════════════════════════
    # HELPERS INTERNES
    # ══════════════════════════════════════════════════════════════════════════

    def _monthly_breakdown(self, qs, qty_field: str, val_field: str) -> list:
        from django.db.models.functions import TruncMonth
        rows = (
            qs.annotate(month=TruncMonth("movement_date"))
            .values("month")
            .annotate(qty=Sum(qty_field), val=Sum(val_field), trans=Count("id"))
            .order_by("month")
        )
        return [
            {
                "month":        r["month"].strftime("%Y-%m") if r["month"] else "?",
                "qty":          self._n(r["qty"]),
                "value":        self._n(r["val"]),
                "transactions": r["trans"] or 0,
            }
            for r in rows
        ]

    # ── Aliases compatibilité ascendante ──────────────────────────────────────

    def get_sales_summary_by_company(self, companies: list, start_date: date, end_date: date) -> list:
        results = []
        for company in companies:
            s = self.get_sales_summary(company, start_date, end_date)
            s["company_name"] = company.name
            s["company_id"]   = str(company.id)
            results.append(s)
        results.sort(key=lambda x: -x["total_revenue"])
        return results

    def get_product_sales_all_companies(self, companies: list, product_name: str,
                                         start_date: date, end_date: date) -> list:
        results = []
        for company in companies:
            s = self.get_product_sales(company, product_name, start_date, end_date)
            if s["total_revenue"] > 0 or s["total_qty"] > 0:
                results.append(s)
        results.sort(key=lambda x: -x["total_revenue"])
        return results

    def get_branches_with_sales(self, company, start_date: date, end_date: date) -> list:
        qs = (
            self._base_qs(company, start_date, end_date, SALES_TYPES)
            .annotate(effective_branch=self._effective_branch_annotation())
            .exclude(effective_branch="")
            .values("effective_branch")
            .distinct()
        )
        return [r["effective_branch"] for r in qs]