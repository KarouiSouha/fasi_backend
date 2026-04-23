"""
apps/ai_insights/services/rag_service.py
-----------------------------------------
Service RAG principal.

CORRECTIONS v5 :
  FIX-CRITIQUE-1 : Suppression de `from flask import ctx` (Flask dans Django !)
  FIX-CRITIQUE-2 : Import correct de settings
  FIX-3 : Nouveau format de contexte "merged" pour LangGraph
  FIX-4 : Toutes les règles intelligence (1-12) conservées
  FIX-5 : Méthode _format() enrichie pour le mode LangGraph
"""

import logging
import re
from django.conf import settings   # FIX-CRITIQUE-2 : était "from Backend.config import settings"
# FIX-CRITIQUE-1 : Suppression de "from flask import ctx" (n'existait pas dans le projet)

from .openai_service import OpenAIService
from .retrieval_service import RetrievalService

logger = logging.getLogger(__name__)


class RagService:
    def __init__(self):
        self.openai    = OpenAIService()
        self.retrieval = RetrievalService()
        self.max_tokens = getattr(settings, "AI_RAG_MAX_RESPONSE_TOKENS", 1200)

    def run(self, question: str, company, context: str, companies: list = None):
        """Point d'entrée principal pour le mode non-LangGraph (rétrocompatibilité)."""
        if not companies:
            companies = [company] if company else []

        retrieval = self.retrieval.build_context(question, company, companies=companies)
        mode      = retrieval.get("mode", "sql")

        logger.debug("[RagService] mode=%s for: %s", mode, question[:80])

        result = self.openai.complete(
            system_prompt=self._system_prompt(),
            user_prompt=self._user_prompt(question, context, retrieval),
            analyzer=f"rag_{mode}",
            max_tokens=self.max_tokens,
        )

        if not result or result.get("error"):
            logger.warning("[RagService] LLM error: %s", result)
            return None

        return result

    # ══════════════════════════════════════════════════════════════════════════
    # SYSTEM PROMPT (conservé intégralement — 12 règles)
    # ══════════════════════════════════════════════════════════════════════════

    def _system_prompt(self) -> str:
        return """You are WEEG, a senior business intelligence analyst for a Libyan technology distributor (Protecta).
You have access to real operational data: sales (ف بيع), purchases (ف شراء), stock, aging receivables, customers, and branches.

═══════════════════════════════════════════════
INTELLIGENCE RULES — ALL MANDATORY
═══════════════════════════════════════════════

RULE 1 — NEVER give a flat "direct answer". Always REASON through 4 layers:
  Layer 1 — Fact:        What does the data say? (1 sentence, with exact number)
  Layer 2 — Context:     Is this good or bad? Trend up/down? Compare to prior period if available.
  Layer 3 — Implication: What risk or opportunity does this create?
  Layer 4 — Action:      One concrete, specific recommended next step.

RULE 2 — CROSS-REFERENCE data automatically when multiple datasets are available.
  • Customer with overdue debt + recent purchases → different risk than one who stopped buying
  • Branch with high stock + zero sales → flag as inefficiency with specific numbers
  • Revenue drop + no data for that month → explain the gap (data cutoff, not a real drop)

RULE 3 — BENCHMARK every number you mention:
  • % of total ("represents 16% of total receivables")
  • Change vs prior period ("down 65% vs February's 1.33M LYD")
  • Daily run-rate if partial month ("March covers only 23 days → 20,090 LYD/day")
  • Ratio ("stock-to-receivables ratio: 2.76:1")

RULE 4 — For FINANCIAL figures, always show the formula for derived metrics:
  "DSO = (2,260,936 / 2,467,438) × 102 days = 93 days"
  "Overdue rate = 2,260,861 / 2,260,936 = 99.997%"

RULE 5 — For STOCK questions:
  • List each branch with quantity AND value
  • zero_stock_count is always ≤ total SKU count
  • Flag zero-stock SKUs as rupture risk
  • Estimate days-of-stock if sales data is available

RULE 6 — For AGING/RECEIVABLES:
  • Show the full bucket breakdown (current, 1-30d, 31-60d, 61-90d, 91d+)
  • Name the top 3 debtors with their exact overdue amounts
  • Calculate DSO = (total_receivables / total_sales_ytd) × days_in_period
  • Give collection priority order

RULE 7 — For NO_DATA situations, DIAGNOSE specifically:
  • Never say "No data found" without explaining WHY.
  • Never describe a data cutoff as a revenue drop.

RULE 8 — DATA INTEGRITY: Use ONLY data provided. Never invent figures.

RULE 9 — MATHEMATICAL SANITY:
  • A percentage "X out of Y" can NEVER exceed 100% when X and Y count the same unit.
  • If raw data suggests >100%, explain the line vs SKU counting difference.

RULE 10 — AGING IMPORT BUG:
  • When aging total > 0 but ALL bucket fields = 0, this is a DATA IMPORT MAPPING BUG.
  • Always state clearly and recommend re-import.

RULE 11 — ARABIC ACCOUNTING TERMINOLOGY:
  • "اعمار الديون" = aging of receivables. NOT the city of Dammam.
  • Explain this clearly when asked.

RULE 12 — FILE CROSS-REFERENCE:
  • When comparing branches across files, use the 5-point structure:
    (1) Branches in official list
    (2) Branches in movements
    (3) Matched (in both)
    (4) In official but no movements
    (5) In movements but not official

═══════════════════════════════════════════════
OUTPUT FORMAT — STRICT
═══════════════════════════════════════════════
Return valid JSON with these exact keys:
{
  "answer": "Rich analytical narrative",
  "decision_needed": true/false,
  "decision_card": null OR {"question": "...", "recommendation": "...", "rationale": "...", "options": [...]},
  "suggested_followups": ["...", "...", "..."],
  "urgency": "low | medium | high | critical",
  "topic": "sales | inventory | aging | customers | purchases | analytical | data_quality | terminology",
  "key_metrics": {"metric_name": "formatted_value"}
}

Answer in the SAME LANGUAGE as the question (French, Arabic, or English).
The 'answer' field must be a plain readable string."""

    # ══════════════════════════════════════════════════════════════════════════
    # USER PROMPT
    # ══════════════════════════════════════════════════════════════════════════

    def _user_prompt(self, question: str, business_context: str, retrieval: dict) -> str:
        mode         = retrieval.get("mode", "sql")
        data_section = self._format(retrieval)
        language = self._detect_question_language(question)

        instructions = (
            "ANALYTICAL TASK:\n"
            "1. Identify the key numbers in the data relevant to this question.\n"
            "2. Apply all 4 reasoning layers: Fact → Context → Implication → Action.\n"
            "3. Cross-reference datasets if multiple are available.\n"
            "4. Benchmark every figure (% of total, trend, comparison).\n"
            "5. For NO_DATA: diagnose WHY — never just say 'no data'.\n"
            "6. RULE 9: Percentages > 100% for proportions are impossible.\n"
            "7. RULE 10: If aging total > 0 but all buckets = 0, diagnose the import bug.\n"
            "8. RULE 11: 'اعمار الدمم' = Arabic accounting term, not Dammam city.\n"
            "9. RULE 12: For branch cross-reference, use the 5-point structure.\n"
            "10. Make the user smarter — not just inform them of a number.\n"
            f"11. LANGUAGE REQUIREMENT: Answer strictly in {language}."
        )

        return "\n\n".join([
            f"QUESTION:\n{question}",
            f"BUSINESS CONTEXT:\n{business_context or 'Libyan technology distributor — B2B sales, multiple branches.'}",
            f"DATA [{mode.upper()}]:\n{data_section}",
            instructions,
        ])

    @staticmethod
    def _detect_question_language(question: str) -> str:
        """Detecte rapidement la langue de la question pour verrouiller la langue de sortie."""
        q = (question or "").strip()
        if not q:
            return "English"

        # Arabic script range
        if re.search(r"[\u0600-\u06FF]", q):
            return "Arabic"

        ql = q.lower()
        french_markers = [
            "quel", "quelle", "quels", "quelles", "pourquoi", "comment",
            "avec", "sans", "liste", "ventes", "branche", "clients", "compte",
        ]
        if any(m in ql for m in french_markers):
            return "French"

        return "English"

    # ══════════════════════════════════════════════════════════════════════════
    # FORMAT ROUTER
    # ══════════════════════════════════════════════════════════════════════════

    def _format(self, ctx: dict) -> str:
        """Route vers le bon formatteur selon le mode de retrieval."""
        if ctx.get("no_data"):
            return (
                f"NO_DATA: {ctx.get('no_data_message', 'Aucune donnée.')}\n"
                f"Available data range: check import logs."
            )

        mode = ctx.get("mode", "sql")

        if mode == "text_to_sql":
            return self._fmt_text_to_sql(ctx)

        if mode == "merged":
            # Nouveau mode LangGraph — le contexte est déjà formaté
            content = ctx.get("content", "")
            enrichment = ctx.get("vector_enrichment", [])
            if enrichment:
                enrichment_text = "\n".join(
                    f"[Semantic {i+1}] {item.get('text', '')[:300]}"
                    for i, item in enumerate(enrichment)
                )
                return f"{content}\n\n[SEMANTIC ENRICHMENT]\n{enrichment_text}"
            return content

        if mode == "hybrid":
            return (
                f"[SQL]\n{self._fmt_sql(ctx.get('sql_context', {}))}\n\n"
                f"[SEMANTIC]\n{self._fmt_vector(ctx.get('vector_context', {}))}"
            )

        formatters = {
            "vector":                 self._fmt_vector,
            "llm_only":               lambda c: c.get("business_summary", "Répondre avec expertise métier."),
            "aging":                  self._fmt_aging,
            "inventory":              self._fmt_inventory,
            "analytical":             self._fmt_analytical,
            "branch_movement_cross":  self._fmt_branch_cross,
            "naming_explanation":     self._fmt_naming_explanation,
            "customer_inactive_debt": self._fmt_customer_inactive_debt,
        }

        formatter = formatters.get(mode, self._fmt_sql)
        return formatter(ctx)

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATTERS SQL
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_sql(self, ctx: dict) -> str:
        if not ctx:
            return "NO_DATA: Aucune donnée SQL."

        lines = []

        if ctx.get("summary"):
            s     = ctx["summary"]
            rev   = s.get("total_revenue", 0)
            trans = s.get("transactions", 0)
            lines += [
                f"Period: {s.get('start_date')} → {s.get('end_date')}",
                f"Revenue:      {rev:>15,.2f} LYD",
                f"Qty sold:     {s.get('total_qty', 0):>15,.0f} units",
                f"Transactions: {trans:>15,}",
                f"Avg ticket:   {rev/trans:,.2f} LYD" if trans else "Avg ticket:   N/A",
            ]
            if ctx.get("monthly_breakdown") or ctx.get("monthly_sales"):
                months = ctx.get("monthly_breakdown") or ctx.get("monthly_sales", [])
                if months:
                    lines.append("Monthly trend:")
                    prev = None
                    for mo in months:
                        r     = mo.get("total_revenue", mo.get("value", 0))
                        arrow = ""
                        if prev is not None and prev > 0:
                            pct   = (r - prev) / prev * 100
                            arrow = f" ({'+' if pct >= 0 else ''}{pct:.0f}%)"
                        lines.append(f"  {mo.get('month', '?')}: {r:,.2f} LYD{arrow}")
                        prev = r

        if ctx.get("all_movements"):
            m = ctx["all_movements"]
            lines += [f"Period: {m.get('start_date')} → {m.get('end_date')}", "Movement types:"]
            labels = {
                "sales":           "Sales (ف بيع)",
                "purchases":       "Purchases (ف شراء)",
                "returns_sale":    "Customer returns (مردودات بيع)",
                "returns_buy":     "Supplier returns (مردود شراء)",
                "transfers_in":    "Transfers IN (نقل)",
                "transfers_out":   "Transfers OUT (نقل)",
                "adjustments_in":  "Adjustments IN (تسوية)",
                "adjustments_out": "Adjustments OUT (تسوية)",
                "damaged":         "Damaged (ف.تالف)",
                "opening_stock":   "Opening stock (ف.أول المدة)",
                "main_in":         "Main entries",
                "main_out":        "Main exits",
                "samples":         "Samples (ف.عينات)",
            }
            for key, label in labels.items():
                d = m.get(key, {})
                if d.get("transactions", 0) > 0 or d.get("value", 0) > 0:
                    lines.append(
                        f"  • {label}: {d.get('transactions',0)} trans | "
                        f"qty={d.get('qty',0):,.0f} | {d.get('value',0):,.2f} LYD"
                    )

        if ctx.get("branch_comparison"):
            lines.append(f"Period: {ctx.get('period', 'N/A')}")
            lines.append("Branch comparison:")
            for b in ctx["branch_comparison"]:
                lines.append(
                    f"  • {b.get('branch_name', 'N/A')}: "
                    f"revenue={b.get('total_revenue', 0):,.2f} LYD | "
                    f"qty={b.get('total_qty', 0):,.0f} | trans={b.get('transactions', 0)}"
                )

        if ctx.get("all_branches"):
            lines.append(f"Period: {ctx.get('period', 'N/A')}")
            lines.append("All branches (by revenue):")
            for i, b in enumerate(ctx["all_branches"], 1):
                lines.append(
                    f"  {i}. {b.get('branch_name', 'N/A')}: "
                    f"{b.get('total_revenue', 0):,.2f} LYD | "
                    f"qty={b.get('total_qty', 0):,.0f} | trans={b.get('transactions', 0)}"
                )

        if ctx.get("branch_overview"):
            bo = ctx["branch_overview"]
            lines += [
                f"Branch: {bo.get('branch_name', 'N/A')} | Period: {bo.get('period', 'N/A')}",
                f"Sales:     {bo.get('sales', {}).get('total_revenue', 0):,.2f} LYD",
                f"Purchases: {bo.get('purchases', {}).get('total_value', 0):,.2f} LYD",
                f"Returns:   {bo.get('returns', {}).get('total_value', 0):,.2f} LYD",
            ]
            if bo.get("inventory"):
                inv  = bo["inventory"]
                sku  = inv.get("sku_count", 1) or 1
                zsk  = inv.get("zero_stock_count", 0)
                zpct = round(zsk / sku * 100, 1)
                lines.append(
                    f"Stock:     {inv.get('total_value', 0):,.2f} LYD | "
                    f"SKUs={sku} | zero_stock_SKUs={zsk} ({zpct}%)"
                )

        if ctx.get("customer_sales"):
            lines.append(f"Period: {ctx.get('period', 'N/A')}")
            lines.append(f"Top {ctx.get('top_n_requested', 10)} customers:")
            for i, c in enumerate(ctx["customer_sales"], 1):
                lines.append(
                    f"  {i}. {c.get('customer_name', 'N/A')}: "
                    f"{c.get('total_revenue', 0):,.2f} LYD | "
                    f"qty={c.get('total_qty', 0):,.0f} | trans={c.get('transactions', 0)}"
                )

        if ctx.get("customer_detail"):
            cd = ctx["customer_detail"]
            s  = cd.get("sales", {})
            r  = cd.get("returns", {})
            lines += [
                f"\nCustomer detail — {cd.get('customer_name', '')}:",
                f"  Sales:   {s.get('total_revenue', 0):,.2f} LYD | qty={s.get('total_qty', 0):,.0f}",
                f"  Returns: {r.get('total_value', 0):,.2f} LYD",
            ]
            if cd.get("top_products"):
                lines.append(
                    f"  Top products: {', '.join(p['material_name'] for p in cd['top_products'][:3])}"
                )

        if ctx.get("customers_stats"):
            cs = ctx["customers_stats"]
            lines.append(
                f"Customers: total={cs.get('total', 0)} | "
                f"active={cs.get('active', 0)} | inactive={cs.get('inactive', 0)}"
            )

        if ctx.get("customer_list"):
            lines.append("Customer list (sample):")
            for c in ctx["customer_list"][:10]:
                lines.append(
                    f"  • {c.get('name', 'N/A')} | "
                    f"code={c.get('account_code', '')} | "
                    f"phone={c.get('phone', '')}"
                )

        if ctx.get("branches"):
            lines.append(f"Branches ({len(ctx['branches'])} total):")
            for b in ctx["branches"]:
                lines.append(f"  • {b.get('name', 'N/A')} | {b.get('address', '')} | {b.get('phone', '')}")

        if ctx.get("product_detail"):
            pd = ctx["product_detail"]
            s  = pd.get("sales", {})
            a  = pd.get("purchases", {})
            lines += [
                f"Product: {pd.get('product_name', 'N/A')} | Period: {pd.get('period', 'N/A')}",
                f"  Sales:     {s.get('total_revenue', 0):,.2f} LYD | qty={s.get('total_qty', 0):,.0f}",
                f"  Purchases: {a.get('total_cost', 0):,.2f} LYD | qty={a.get('total_qty', 0):,.0f}",
                f"  Margin:    {pd.get('gross_margin', 0):,.2f} LYD ({pd.get('gross_margin_pct', 0):.1f}%)",
            ]
            if pd.get("by_branch"):
                lines.append("  By branch (sales):")
                for b in pd["by_branch"][:5]:
                    lines.append(
                        f"    • {b.get('branch_name', 'N/A')}: "
                        f"{b.get('total_revenue', 0):,.2f} LYD"
                    )
            if pd.get("current_stock") and pd["current_stock"].get("by_branch"):
                lines.append("  Current stock by branch:")
                for b in pd["current_stock"]["by_branch"]:
                    status = "✓" if b.get("quantity", 0) > 0 else "✗ OUT OF STOCK"
                    lines.append(
                        f"    {status} {b.get('branch_name', 'N/A')}: "
                        f"qty={b.get('quantity', 0):,.0f}"
                    )

        if ctx.get("top_products"):
            lines.append("Top products by revenue:")
            for i, p in enumerate(ctx["top_products"][:10], 1):
                lines.append(
                    f"  {i}. [{p.get('material_code', '')}] {p.get('material_name', 'N/A')}: "
                    f"{p.get('total_revenue', 0):,.2f} LYD"
                )

        if ctx.get("purchases_summary"):
            ps = ctx["purchases_summary"]
            lines += [
                f"Purchases — Period: {ps.get('start_date')} → {ps.get('end_date')}",
                f"  Total: {ps.get('total_value', 0):,.2f} LYD | trans={ps.get('transactions', 0)}",
            ]

        if ctx.get("top_suppliers"):
            lines.append("Top suppliers:")
            for i, s in enumerate(ctx["top_suppliers"][:5], 1):
                lines.append(
                    f"  {i}. {s.get('supplier_name', 'N/A')}: {s.get('total_value', 0):,.2f} LYD"
                )

        if ctx.get("returns_sale"):
            rs = ctx["returns_sale"]
            lines.append(
                f"Customer returns (مردودات بيع): {rs.get('total_value', 0):,.2f} LYD | "
                f"trans={rs.get('transactions', 0)}"
            )

        if ctx.get("returns_buy"):
            rb = ctx["returns_buy"]
            lines.append(
                f"Supplier returns (مردود شراء): {rb.get('total_value', 0):,.2f} LYD | "
                f"trans={rb.get('transactions', 0)}"
            )

        if ctx.get("transfers"):
            tf = ctx["transfers"]
            lines.append(
                f"Transfers (نقل): "
                f"in={tf.get('transfers_in', {}).get('value', 0):,.2f} LYD | "
                f"out={tf.get('transfers_out', {}).get('value', 0):,.2f} LYD"
            )

        if ctx.get("damaged"):
            d = ctx["damaged"]
            lines.append(
                f"Damaged (ف.تالف): {d.get('total_value', 0):,.2f} LYD | "
                f"qty={d.get('total_qty', 0):,.0f}"
            )
            if d.get("items"):
                for item in d["items"][:5]:
                    lines.append(
                        f"  • {item.get('product', 'N/A')} @ {item.get('branch', '')} "
                        f"({item.get('date', '')}): qty={item.get('qty', 0):,.0f}"
                    )

        if ctx.get("opening_stock"):
            op = ctx["opening_stock"]
            lines.append(
                f"Opening stock (ف.أول المدة): {op.get('total_value', 0):,.2f} LYD | "
                f"qty={op.get('total_qty', 0):,.0f}"
            )

        if ctx.get("margin"):
            mg = ctx["margin"]
            lines += [
                f"Gross margin — Period: {mg.get('start_date')} → {mg.get('end_date')}",
                f"  Net revenue:  {mg.get('net_revenue', 0):,.2f} LYD",
                f"  Net cost:     {mg.get('net_cost', 0):,.2f} LYD",
                f"  Gross margin: {mg.get('gross_margin', 0):,.2f} LYD ({mg.get('gross_margin_pct', 0):.1f}%)",
            ]

        if ctx.get("monthly_sales"):
            lines.append("Monthly sales:")
            prev = None
            for mo in ctx["monthly_sales"]:
                rev   = mo.get("total_revenue", 0)
                arrow = ""
                if prev is not None and prev > 0:
                    pct   = (rev - prev) / prev * 100
                    arrow = f" ({'+' if pct >= 0 else ''}{pct:.0f}%)"
                lines.append(
                    f"  {mo.get('month', '?')}: {rev:,.2f} LYD | "
                    f"trans={mo.get('transactions', 0)}{arrow}"
                )
                prev = rev

        if ctx.get("category_sales"):
            lines.append("Sales by category:")
            for c in ctx["category_sales"][:10]:
                lines.append(
                    f"  • {c.get('category', 'N/A')}: {c.get('total_revenue', 0):,.2f} LYD"
                )

        if ctx.get("adjustments"):
            adj = ctx["adjustments"]
            lines.append(
                f"Stock adjustments (تسوية): "
                f"in={adj.get('adjustments_in', {}).get('value', 0):,.2f} LYD | "
                f"out={adj.get('adjustments_out', {}).get('value', 0):,.2f} LYD"
            )

        if ctx.get("top_purchased"):
            lines.append("Top purchased products:")
            for i, p in enumerate(ctx["top_purchased"][:10], 1):
                lines.append(
                    f"  {i}. {p.get('material_name', 'N/A')}: "
                    f"qty={p.get('total_qty', 0):,.0f} | {p.get('total_value', 0):,.2f} LYD"
                )

        return "\n".join(lines) if lines else "NO_DATA: Aucune donnée pour cette requête."

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATTERS SPÉCIALISÉS (conservés de v4)
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_aging(self, ctx: dict) -> str:
        if ctx.get("no_data"):
            return f"NO_DATA: {ctx.get('no_data_message', 'Aucune créance.')}"

        ag = ctx.get("aging_summary")
        if not ag:
            return (
                "NO_DATA: Aucune donnée de créances.\n"
                "DIAGNOSTIC: Vérifier que le fichier اعمار__الدمم__2026.xlsx a été importé."
            )

        total   = ag.get("total", 0)
        current = ag.get("current", 0)
        overdue = ag.get("overdue_total", 0)

        has_import_bug = ctx.get("has_import_bug", False)
        if not has_import_bug:
            bucket_sum = sum(ag.get(k, 0) for k in [
                "d1_30", "d31_60", "d61_90", "d91_120",
                "d121_150", "d151_180", "over_180", "current"
            ])
            has_import_bug = (total > 0 and bucket_sum == 0)

        lines = [
            f"Report date: {ctx.get('report_date', 'N/A')}",
            f"Total receivables: {total:,.2f} LYD",
        ]

        if has_import_bug:
            lines += [
                "",
                "⚠ IMPORT COLUMN-MAPPING BUG DETECTED:",
                f"  Total receivable = {total:,.2f} LYD — this figure IS reliable.",
                "  BUT all aging bucket fields show 0 LYD — Excel column mapping issue.",
                "  ACTION REQUIRED: re-import file after verifying column order.",
                "",
            ]
        else:
            lines += [
                f"Current (not due):  {current:,.2f} LYD",
                f"OVERDUE TOTAL:      {overdue:,.2f} LYD ({ag.get('pct_overdue', 0):.1f}%)",
                "",
                "Aging bucket breakdown:",
                f"  1–30 days:    {ag.get('d1_30',    0):>12,.2f} LYD",
                f"  31–60 days:   {ag.get('d31_60',   0):>12,.2f} LYD",
                f"  61–90 days:   {ag.get('d61_90',   0):>12,.2f} LYD",
                f"  91–120 days:  {ag.get('d91_120',  0):>12,.2f} LYD  ← URGENT",
                f"  121–150 days: {ag.get('d121_150', 0):>12,.2f} LYD",
                f"  151–180 days: {ag.get('d151_180', 0):>12,.2f} LYD",
                f"  >180 days:    {ag.get('over_180', 0):>12,.2f} LYD  ← WRITE-OFF RISK",
            ]

        lines.append(f"  Total accounts: {ag.get('account_count', 0)}")

        if ctx.get("top_accounts"):
            lines.append("\nTop accounts by total exposure:")
            for acc in ctx["top_accounts"][:5]:
                if has_import_bug:
                    lines.append(
                        f"  • {acc.get('account', 'N/A')}: "
                        f"total={acc.get('total', 0):,.2f} LYD "
                        f"(bucket breakdown unavailable — import bug)"
                    )
                else:
                    lines.append(
                        f"  • {acc.get('account', 'N/A')}: "
                        f"total={acc.get('total', 0):,.2f} LYD | "
                        f"overdue={max(0, acc.get('total',0) - acc.get('current',0)):,.2f} LYD"
                    )

        return "\n".join(lines)

    def _fmt_inventory(self, ctx: dict) -> str:
        if ctx.get("no_data"):
            return f"NO_DATA: {ctx.get('no_data_message', 'Aucune donnée stock.')}"

        if not ctx or not ctx.get("summary"):
            return (
                "NO_DATA: InventorySnapshotLine est vide.\n"
                "DIAGNOSTIC: Le fichier جرد__افقي__2026.xlsx n'a pas encore été importé."
            )

        inv  = ctx["summary"]
        lines = []

        sku_count        = inv.get("sku_count", 0) or 1
        total_lines      = inv.get("total_lines", 0)
        zero_stock_skus  = inv.get("zero_stock_count", 0)
        zero_pct         = round(zero_stock_skus / sku_count * 100, 1)

        lines += [
            f"Total unique SKUs:        {sku_count:,}",
            f"Total inventory lines:    {total_lines:,} (1 line per SKU per branch)",
            f"Total quantity:           {inv.get('total_qty', 0):,.0f} units",
            f"Total value:              {inv.get('total_value', 0):,.2f} LYD",
            f"Zero-stock SKUs:          {zero_stock_skus:,} ({zero_pct:.1f}% of SKUs)",
            "NOTE: zero-stock lines > unique SKUs is expected (each SKU × N branches).",
        ]

        if ctx.get("by_branch"):
            lines.append("\nStock by branch:")
            for b in ctx["by_branch"]:
                lines.append(
                    f"  • {b['branch_name']}: "
                    f"value={b.get('total_value', 0):,.2f} LYD | "
                    f"qty={b.get('total_qty', 0):,.0f} | "
                    f"SKUs={b.get('sku_count', 0)} | "
                    f"zero_SKUs={b.get('zero_stock', 0)} ({b.get('zero_pct', 0):.0f}%)"
                )

        if ctx.get("out_of_stock"):
            lines.append(f"\nOut-of-stock items ({len(ctx['out_of_stock'])}):")
            for p in ctx["out_of_stock"][:8]:
                lines.append(
                    f"  ✗ [{p.get('product_code', '')}] {p.get('product_name', 'N/A')} "
                    f"@ {p.get('branch_name', '')}"
                )

        return "\n".join(lines)

    def _fmt_analytical(self, ctx: dict) -> str:
        sections = ctx.get("sections", {})
        parts    = []

        if "sales" in sections:
            s  = sections["sales"]
            sm = s.get("summary", {})
            rev   = sm.get("total_revenue", 0)
            trans = sm.get("transactions", 0)
            block = [
                f"[SALES] Period: {sm.get('start_date')} → {sm.get('end_date')}",
                f"  Revenue:      {rev:,.2f} LYD",
                f"  Transactions: {trans:,}",
            ]
            if trans:
                block.append(f"  Avg ticket:   {rev/trans:,.2f} LYD")
            if s.get("monthly"):
                block.append("  Monthly trend:")
                prev = None
                for mo in s["monthly"]:
                    r     = mo.get("total_revenue", 0)
                    arrow = ""
                    if prev is not None and prev > 0:
                        pct   = (r - prev) / prev * 100
                        arrow = f" ({'+' if pct >= 0 else ''}{pct:.0f}%)"
                    block.append(f"    {mo['month']}: {r:,.2f} LYD{arrow}")
                    prev = r
            if s.get("top_customers"):
                block.append("  Top customers:")
                for c in s["top_customers"][:3]:
                    pct = c.get("total_revenue", 0) / rev * 100 if rev else 0
                    block.append(
                        f"    {c['customer_name']}: {c.get('total_revenue', 0):,.2f} LYD ({pct:.1f}%)"
                    )
            parts.append("\n".join(filter(None, block)))

        if "purchases" in sections:
            p  = sections["purchases"]
            ps = p.get("summary", {})
            block = [
                f"[PURCHASES] Total: {ps.get('total_value', 0):,.2f} LYD | "
                f"trans={ps.get('transactions', 0)}",
            ]
            if p.get("top_suppliers"):
                block.append("  Top suppliers:")
                for sup in p["top_suppliers"][:3]:
                    block.append(f"    {sup['supplier_name']}: {sup['total_value']:,.2f} LYD")
            parts.append("\n".join(block))

        if "margin" in sections:
            mg = sections["margin"]
            parts.append(
                f"[MARGIN]\n"
                f"  Net revenue:  {mg.get('net_revenue', 0):,.2f} LYD\n"
                f"  Net cost:     {mg.get('net_cost', 0):,.2f} LYD\n"
                f"  Gross margin: {mg.get('gross_margin', 0):,.2f} LYD "
                f"({mg.get('gross_margin_pct', 0):.1f}%)"
            )

        if "aging" in sections:
            parts.append(f"[RECEIVABLES]\n{self._fmt_aging(sections['aging'])}")

        if "inventory" in sections:
            parts.append(f"[INVENTORY]\n{self._fmt_inventory(sections['inventory'])}")

        if "returns" in sections:
            rs = sections["returns"].get("sale", {})
            rb = sections["returns"].get("buy", {})
            parts.append(
                f"[RETURNS]\n"
                f"  Customer: {rs.get('total_value', 0):,.2f} LYD | trans={rs.get('transactions', 0)}\n"
                f"  Supplier: {rb.get('total_value', 0):,.2f} LYD | trans={rb.get('transactions', 0)}"
            )

        if "customers" in sections:
            c = sections["customers"]
            parts.append(
                f"[CUSTOMERS] total={c.get('total', 0)} | "
                f"active={c.get('active', 0)} | inactive={c.get('inactive', 0)}"
            )

        return "\n\n".join(parts) if parts else "NO_DATA: Aucune donnée analytique."

    def _fmt_branch_cross(self, ctx: dict) -> str:
        cross = ctx.get("cross", {})
        lines = [
            "=== BRANCH CROSS-REFERENCE (RULE 12) ===",
            f"Period: {ctx.get('period', 'N/A')}",
            "",
            f"Official branches: {cross.get('official_count', 0)}",
            f"Branches in movements: {cross.get('movement_count', 0)}",
            "",
            "MATCHED (in both):",
        ]
        for b in cross.get("matched", []):
            lines.append(f"  ✓ {b}")
        if not cross.get("matched"):
            lines.append("  (none)")

        lines.append("\nIN OFFICIAL LIST but NO movements:")
        for b in cross.get("in_official_not_movement", []):
            lines.append(f"  ✗ {b}  ← no transactions recorded")
        if not cross.get("in_official_not_movement"):
            lines.append("  (none — all official branches have movements)")

        lines.append("\nIN MOVEMENTS but NOT in official list:")
        for b in cross.get("in_movement_not_official", []):
            lines.append(f"  ? {b}  ← ghost branch")
        if not cross.get("in_movement_not_official"):
            lines.append("  (none)")

        return "\n".join(lines)

    def _fmt_naming_explanation(self, ctx: dict) -> str:
        return (
            "=== ARABIC ACCOUNTING TERMINOLOGY ===\n\n"
            "Term: اعمار الدمم / اعمار الذمم / أعمار الذمم المدينة\n\n"
            "ETYMOLOGY:\n"
            "  Root: الذِمَّة (al-dhimma) = financial obligation, liability\n"
            "  Plural: الذِمَم or الدِمَم (both correct in Classical Arabic)\n"
            "  Full form: أعمار الذمم المدينة = aging of accounts receivable\n"
            "  ERP abbreviation: اعمار الدمم (used in Libyan/Gulf Arabic ERP)\n\n"
            "IMPORTANT:\n"
            "  الدمام (al-Dammam) = Saudi city — spelled with long ā: دَامَّ\n"
            "  الدِمَم (al-dimam)  = debts/obligations — spelled with kasra: دِمَم\n"
            "  These are DIFFERENT words with different vowels and meaning.\n\n"
            "CONCLUSION:\n"
            "  The file اعمار__الدمم__2026.xlsx is an ACCOUNTS RECEIVABLE AGING report.\n"
            "  It has NO connection to the city of Dammam.\n"
            f"\nBusiness context: {ctx.get('business_context', 'Libyan distribution company')}"
        )

    def _fmt_customer_inactive_debt(self, ctx: dict) -> str:
        no_sales = ctx.get("no_sales_customers", [])
        aging    = ctx.get("aging_data", {})
        period   = ctx.get("period", "N/A")
        cs       = ctx.get("customers_stats", {})
        ag_sum   = (aging.get("aging_summary") or {})
        total_debt = ag_sum.get("total", 0)
        has_bug    = aging.get("has_import_bug", False)

        lines = [
            "=== CLIENTS WITH DEBT BUT NO RECENT SALES ===",
            f"Period checked: {period}",
            f"Total receivables: {total_debt:,.2f} LYD",
            f"Customers: total={cs.get('total',0)} | active={cs.get('active',0)}",
            "",
        ]

        if has_bug:
            lines.append("⚠ Aging import bug active — individual bucket amounts unreliable.\n")

        if not no_sales:
            lines.append(
                "NO_DATA: Could not identify customers with debt and no sales.\n"
                "REASON: Naming mismatch between aging (account names) and movements (customer_name).\n"
                "RECOMMENDED ACTION: Add account_code join key to both models."
            )
        else:
            lines.append("Results (sorted by: no-sales first, then by debt):")
            for r in no_sales[:15]:
                sales_info = (
                    f"last sale: {r['sales_value']:,.2f} LYD" if r["has_sales"]
                    else "NO SALES in period"
                )
                lines.append(
                    f"  {'✗' if not r['has_sales'] else '~'} {r['account']}: "
                    f"debt={r['total_debt']:,.2f} LYD | {sales_info}"
                )

        return "\n".join(lines)

    def _fmt_vector(self, ctx: dict) -> str:
        items = ctx.get("items") or []
        if not items:
            return "NO_DATA: No semantic results — Qdrant may be empty or not configured."
        return "\n".join(
            f"[{i+1}] {item.get('text', '')[:400]} (score={item.get('score', 0):.3f})"
            for i, item in enumerate(items[:6])
        )

    def _fmt_text_to_sql(self, ctx: dict) -> str:
        t2s = ctx.get("text_to_sql", {})
        if not t2s.get("success"):
            return f"TEXT-TO-SQL FAILED: {t2s.get('error', 'unknown')}"

        lines = [
            "=== TEXT-TO-SQL RESULT ===",
            f"Query interpreted: {t2s.get('explanation', '')}",
            f"Confidence: {t2s.get('confidence', '?')}",
            f"Rows: {t2s.get('row_count', 0)}",
            "",
            ctx.get("prompt_context", ""),
        ]
        return "\n".join(lines)