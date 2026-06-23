"""
apps/data_import/services/excel_vector_ingest.py
-------------------------------------------------
Universal Vector Ingest Service — all Excel file types.

Supports:
  - movements   : ALL types (sales, purchases, transfers, adjustments...)
  - aging       : receivables by aging bucket
  - inventory   : stock by product and branch
  - customers   : customer profile + account code
  - branches    : branch list

Architecture:
  - One text builder per file type (_build_*_text)
  - Batch processing in sets of 20 to avoid Qdrant Cloud timeouts
  - 0.5s pause between batches for network stability
  - Extensible: adding a new type = adding a _build_*_text method
"""

import logging
import time
from datetime import date

logger = logging.getLogger(__name__)

BATCH_SIZE = 20


class ExcelVectorIngestService:

    def __init__(self):
        from apps.ai_insights.services.qdrant_service import (
            QdrantService, QdrantServiceUnavailable
        )
        from apps.ai_insights.services.openai_service import OpenAIService

        self.openai = OpenAIService()
        try:
            self.qdrant = QdrantService()
        except QdrantServiceUnavailable as exc:
            self.qdrant = None
            logger.warning("[VectorIngest] Qdrant unavailable: %s", exc)

    # ── Main entry point ──────────────────────────────────────────────────────

    def ingest(self, company, file_type: str, date_range=None):
        """
        Universal entry point — called after each Excel import.

        Args:
            company   : Django Company instance
            file_type : 'movements' | 'aging' | 'inventory' | 'customers' | 'branches'
            date_range: [date_from, date_to] for movements only
        """
        if not self.qdrant:
            logger.warning("[VectorIngest] Qdrant unavailable — indexing skipped.")
            return

        dispatcher = {
            "movements": self._ingest_movements,
            "aging":     self._ingest_aging,
            "inventory": self._ingest_inventory,
            "customers": self._ingest_customers,
            "branches":  self._ingest_branches,
        }

        handler = dispatcher.get(file_type)
        if not handler:
            logger.warning("[VectorIngest] Type '%s' not supported.", file_type)
            return

        logger.info("[VectorIngest] Starting indexing type=%s company=%s", file_type, company.id)
        handler(company, date_range=date_range)

    # ── Backward compatibility method ─────────────────────────────────────────

    def index_movements(self, company, date_range=None):
        """Backward compatibility with the legacy call in views.py."""
        self.ingest(company, "movements", date_range=date_range)

    # ── MOVEMENTS — ALL movement types ────────────────────────────────────────

    def _ingest_movements(self, company, date_range=None, **kwargs):
        from apps.transactions.models import MaterialMovement

        # All movement types (not just sales)
        query = MaterialMovement.objects.filter(company=company)

        if date_range and len(date_range) == 2 and date_range[0] and date_range[1]:
            query = query.filter(
                movement_date__gte=date_range[0],
                movement_date__lte=date_range[1],
            )
        else:
            today = date.today()
            past_year = date(today.year - 1, today.month, today.day)
            query = query.filter(movement_date__gte=past_year)

        rows = list(query.select_related("branch").order_by("movement_date")[:2000])
        if not rows:
            logger.info("[VectorIngest] No movements to index.")
            return

        texts = [self._build_movement_text(m) for m in rows]
        ids   = [str(m.id) for m in rows]
        payloads = [
            {
                "company_id":    str(company.id),
                "file_type":     "movements",
                "movement_date": m.movement_date.isoformat(),
                "movement_type": m.movement_type or "",
                "material_name": m.material_name or "",
                "material_code": m.material_code or "",
                "customer_name": m.customer_name or "",
                "branch":        m.branch.name if m.branch else "",
                "qty_in":        float(m.qty_in or 0),
                "qty_out":       float(m.qty_out or 0),
                "total_in":      float(m.total_in or 0),
                "total_out":     float(m.total_out or 0),
            }
            for m in rows
        ]
        self._batch_upsert(texts, ids, payloads, label="movements")

    @staticmethod
    def _build_movement_text(m) -> str:
        direction = "IN" if (m.qty_in and float(m.qty_in) > 0) else "OUT"
        qty   = float(m.qty_in or 0) if direction == "IN" else float(m.qty_out or 0)
        total = float(m.total_in or 0) if direction == "IN" else float(m.total_out or 0)
        return (
            f"Type: {m.movement_type or 'N/A'} | "
            f"Date: {m.movement_date.isoformat()} | "
            f"Direction: {direction} | "
            f"Product: {m.material_name or m.material_code or 'N/A'} | "
            f"Code: {m.material_code or ''} | "
            f"Qty: {qty:.2f} | "
            f"Amount: {total:.2f} LYD | "
            f"Branch: {m.branch.name if m.branch else 'N/A'} | "
            f"Customer: {m.customer_name or 'N/A'}"
        )

    # ── AGING RECEIVABLES ─────────────────────────────────────────────────────

    def _ingest_aging(self, company, **kwargs):
        from apps.aging.models import AgingReceivable, AgingSnapshot

        snapshot = (
            AgingSnapshot.objects.filter(company=company)
            .order_by("-uploaded_at").first()
        )
        if not snapshot:
            logger.info("[VectorIngest] No aging snapshot found.")
            return

        records = list(AgingReceivable.objects.filter(snapshot=snapshot))
        if not records:
            return

        texts    = [self._build_aging_text(r) for r in records]
        ids      = [f"aging-{r.id}" for r in records]
        payloads = [
            {
                "company_id":    str(company.id),
                "file_type":     "aging",
                "snapshot_id":   str(snapshot.id),
                "aging_year":    snapshot.aging_year,
                "account":       r.account or "",
                "account_code":  r.account_code or "",
                "total":         float(r.total or 0),
                "current":       float(r.current or 0),
                "overdue_total": float(r.overdue_total or 0),
                "risk_score":    r.risk_score or "",
            }
            for r in records
        ]
        self._batch_upsert(texts, ids, payloads, label="aging")

    @staticmethod
    def _build_aging_text(r) -> str:
        total   = float(r.total or 0)
        current = float(r.current or 0)
        overdue = float(r.overdue_total or 0)
        pct     = round(overdue / total * 100, 1) if total > 0 else 0
        return (
            f"Account: {r.account or r.account_code or 'N/A'} | "
            f"Total receivable: {total:,.2f} LYD | "
            f"Current: {current:,.2f} LYD | "
            f"Overdue: {overdue:,.2f} LYD ({pct}%) | "
            f"1-30d: {float(r.d1_30 or 0):,.2f} | "
            f"31-60d: {float(r.d31_60 or 0):,.2f} | "
            f"61-90d: {float(r.d61_90 or 0):,.2f} | "
            f"91-120d: {float(r.d91_120 or 0):,.2f} | "
            f"Over 330d: {float(r.over_330 or 0):,.2f} | "
            f"Risk: {r.risk_score or 'unknown'}"
        )

    # ── INVENTORY ─────────────────────────────────────────────────────────────

    def _ingest_inventory(self, company, **kwargs):
        from apps.inventory.models import InventorySnapshotLine

        lines = list(
            InventorySnapshotLine.objects.filter(company=company)
            .order_by("product_code", "branch_name")[:3000]
        )
        if not lines:
            logger.info("[VectorIngest] No inventory lines found.")
            return

        texts    = [self._build_inventory_text(l) for l in lines]
        ids      = [f"inv-{l.id}" for l in lines]
        payloads = [
            {
                "company_id":       str(company.id),
                "file_type":        "inventory",
                "product_code":     l.product_code or "",
                "product_name":     l.product_name or "",
                "product_category": l.product_category or "",
                "branch_name":      l.branch_name or "",
                "quantity":         float(l.quantity or 0),
                "unit_cost":        float(l.unit_cost or 0),
                "line_value":       float(l.line_value or 0),
                "inventory_year":   l.inventory_year or 0,
            }
            for l in lines
        ]
        self._batch_upsert(texts, ids, payloads, label="inventory")

    @staticmethod
    def _build_inventory_text(l) -> str:
        return (
            f"Product: {l.product_name or l.product_code or 'N/A'} | "
            f"Code: {l.product_code or ''} | "
            f"Category: {l.product_category or 'N/A'} | "
            f"Branch: {l.branch_name or 'N/A'} | "
            f"Quantity: {float(l.quantity or 0):,.2f} units | "
            f"Unit cost: {float(l.unit_cost or 0):,.2f} LYD | "
            f"Total value: {float(l.line_value or 0):,.2f} LYD | "
            f"Year: {l.inventory_year or 'N/A'}"
        )

    # ── CUSTOMERS ─────────────────────────────────────────────────────────────

    def _ingest_customers(self, company, **kwargs):
        from apps.customers.models import Customer

        customers = list(Customer.objects.filter(company=company, is_active=True)[:1000])
        if not customers:
            logger.info("[VectorIngest] No customers found.")
            return

        texts    = [self._build_customer_text(c) for c in customers]
        ids      = [f"cust-{c.id}" for c in customers]
        payloads = [
            {
                "company_id":   str(company.id),
                "file_type":    "customers",
                "customer_id":  str(c.id),
                "account_code": c.account_code or "",
                "name":         c.name or "",
                "area_code":    c.area_code or "",
                "is_active":    c.is_active,
            }
            for c in customers
        ]
        self._batch_upsert(texts, ids, payloads, label="customers")

    @staticmethod
    def _build_customer_text(c) -> str:
        return (
            f"Customer: {c.name or 'N/A'} | "
            f"Account code: {c.account_code or 'N/A'} | "
            f"Area: {c.area_code or 'N/A'} | "
            f"Phone: {c.phone or 'N/A'} | "
            f"Address: {c.address or 'N/A'} | "
            f"Active: {'Yes' if c.is_active else 'No'}"
        )

    # ── BRANCHES ──────────────────────────────────────────────────────────────

    def _ingest_branches(self, company, **kwargs):
        from apps.branches.models import Branch

        branches = list(Branch.objects.filter(is_active=True)[:200])
        if not branches:
            logger.info("[VectorIngest] No branches found.")
            return

        texts    = [self._build_branch_text(b) for b in branches]
        ids      = [f"branch-{b.id}" for b in branches]
        payloads = [
            {
                "company_id": str(company.id),
                "file_type":  "branches",
                "branch_id":  str(b.id),
                "name":       b.name or "",
                "address":    b.address or "",
                "phone":      b.phone or "",
                "is_active":  b.is_active,
            }
            for b in branches
        ]
        self._batch_upsert(texts, ids, payloads, label="branches")

    @staticmethod
    def _build_branch_text(b) -> str:
        return (
            f"Branch: {b.name or 'N/A'} | "
            f"Address: {b.address or 'N/A'} | "
            f"Phone: {b.phone or 'N/A'} | "
            f"Active: {'Yes' if b.is_active else 'No'}"
        )

    # ── Common batch upsert method ────────────────────────────────────────────

    def _batch_upsert(self, texts: list, ids: list, payloads: list, label: str):
        """
        Sends vectors to Qdrant in batches of BATCH_SIZE.
        Per-batch error handling — a failed batch does not stop the remaining ones.
        """
        total_indexed = 0
        total_batches = (len(texts) - 1) // BATCH_SIZE + 1

        for batch_start in range(0, len(texts), BATCH_SIZE):
            batch_texts    = texts[batch_start: batch_start + BATCH_SIZE]
            batch_ids      = ids[batch_start: batch_start + BATCH_SIZE]
            batch_payloads = payloads[batch_start: batch_start + BATCH_SIZE]
            batch_num      = batch_start // BATCH_SIZE + 1

            # Embedding generation
            try:
                vectors = self.openai.embed_texts(batch_texts)
            except Exception as exc:
                logger.warning(
                    "[VectorIngest] Embedding failed batch %d/%d (%s): %s",
                    batch_num, total_batches, label, exc
                )
                continue

            # Build Qdrant points
            points = [
                {
                    "id":      bid,
                    "vector":  vec,
                    "payload": {**payload, "text": text},
                }
                for bid, vec, text, payload
                in zip(batch_ids, vectors, batch_texts, batch_payloads)
            ]

            # Upsert into Qdrant
            try:
                self.qdrant.upsert_documents(points)
                total_indexed += len(points)
                time.sleep(0.5)
            except Exception as exc:
                logger.warning(
                    "[VectorIngest] Qdrant upsert failed batch %d/%d (%s): %s",
                    batch_num, total_batches, label, exc
                )
                continue

        logger.info(
            "[VectorIngest] ✅ %s — %d/%d documents indexed into Qdrant.",
            label, total_indexed, len(texts)
        )