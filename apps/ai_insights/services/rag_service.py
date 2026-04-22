"""
apps/ai_insights/services/rag_service.py
-----------------------------------------
CORRECTIONS v4 :
  FIX-1 : System prompt → 4 nouvelles règles intelligence
  FIX-2 : _fmt_aging() → bug d'import TOUJOURS visible dans la réponse,
           jamais de réponse contradictoire (total > 0 mais buckets = 0)
  FIX-3 : _fmt_inventory() → zero_stock_count = SKUs uniques (pas lignes),
           % toujours ≤ 100%
  FIX-4 : _fmt_branch_cross() [NOUVEAU] — croisement branches officielles
           vs branches dans les mouvements
  FIX-5 : _fmt_naming_explanation() [NOUVEAU] — terminologie comptable arabe
           "اعمار الديون" ≠ ville de Dammam
  FIX-6 : _fmt_customer_inactive_debt() [NOUVEAU] — clients avec dette
           mais sans transaction récente
"""

import logging

from django.conf import settings
from flask import ctx

from .openai_service import OpenAIService
from .retrieval_service import RetrievalService

logger = logging.getLogger(__name__)


class RagService:
    def __init__(self):
        self.openai    = OpenAIService()
        self.retrieval = RetrievalService()
        self.max_tokens = getattr(settings, "AI_RAG_MAX_RESPONSE_TOKENS", 1200)

    def run(self, question: str, company, context: str, companies: list = None):
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
    # SYSTEM PROMPT
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
  • zero_stock_count is always ≤ total SKU count — if data shows otherwise, it is a data counting issue (lines vs SKUs), explain this
  • Flag zero-stock SKUs as rupture risk
  • Estimate days-of-stock if sales data is available

RULE 6 — For AGING/RECEIVABLES:
  • Show the full bucket breakdown (current, 1-30d, 31-60d, 61-90d, 91d+)
  • Name the top 3 debtors with their exact overdue amounts
  • Calculate DSO = (total_receivables / total_sales_ytd) × days_in_period
  • Give collection priority order

RULE 7 — For NO_DATA situations, DIAGNOSE specifically:
  • "April 2026 shows 0 LYD sales because data in the system only goes to March 23, 2026 — this is a data cutoff, not a business drop."
  • Never say "No data found" without explaining WHY.
  • Never describe a data cutoff as a "chute de 91%"  — that is factually wrong and alarmist.

RULE 8 — DATA INTEGRITY: Use ONLY data provided. Never invent figures.
  If a number seems wrong (all buckets = 0 despite total > 0), flag it as a likely import mapping issue.

RULE 9 — MATHEMATICAL SANITY [NOUVEAU] :
  • A percentage of the form "X out of Y" can NEVER exceed 100% when X and Y count the same unit.
  • "zero_stock SKUs / total SKUs" must ALWAYS be between 0% and 100%.
  • If raw data suggests a percentage > 100% (e.g. 327%), it means the numerator counts LINES
    while the denominator counts UNIQUE SKUs — they are different units. ALWAYS flag this:
    "The system counted 4,937 inventory lines at zero quantity across 1,509 unique SKUs
    (average 3.3 lines/SKU since each SKU appears once per branch).
    The correct zero-stock rate is: X unique SKUs out of 1,509 = Y%."
  • Never output a percentage > 100% for a proportion without explaining the counting difference.

RULE 10 — AGING IMPORT BUG [NOUVEAU] :
  • When aging data shows total > 0 LYD but ALL bucket fields (current, 1-30d, 31-60d …) = 0,
    this is a DATA IMPORT MAPPING BUG, not reality.
  • ALWAYS state clearly: "The total receivable of X LYD is confirmed real.
    However, the aging bucket breakdown (current / 1-30d / 31-60d / etc.) shows all zeros —
    this is an import column-mapping issue: the Excel parser reads columns by position,
    and if the source file column order differs from the expected template,
    all bucket values land in the wrong fields.
    Action required: re-import the اعمار__الدمم__2026.xlsx file after verifying column order."
  • NEVER contradict yourself by saying "0 LYD in all buckets" AND "total = 2.26M LYD" in the
    same response without the above explanation.

RULE 11 — ARABIC ACCOUNTING TERMINOLOGY [NOUVEAU] :
  • "اعمار الديون" or "أعمار الدمم" is a standard Arabic accounting term meaning
    "aging of debts/receivables". It is commonly written as "اعمار الذمم" or
    abbreviated as "اعمار الدمم" in Libyan and Gulf ERP systems (Odoo, Epicor, AS400).
  • "الدمم" in this accounting context = الديون / الذمم (obligations, debts).
    It has NO connection to the Saudi city of Dammam (الدمام).
  • When a user asks why a Libyan company's file is named "اعمار_الدمم", explain:
    "This is a standard Arabic bookkeeping term — 'اعمار الذمم المدينة' (aging of receivables)
    is often shortened to 'اعمار الدمم' in Arabic ERP templates.
    The word 'الدمم' here means 'obligations/debts' (from الذمة = financial obligation),
    not the Saudi city of Dammam (الدمام which is spelled differently)."

RULE 12 — FILE CROSS-REFERENCE [NOUVEAU] :
  • When asked to compare data across two named files (e.g., branches in فروع_بروتكتا vs
    branches in حركة_المادة), ALWAYS structure the answer as:
    (1) How many branches in file A (the official list)?
    (2) How many distinct branches appear in file B (the movements)?
    (3) Which branches are in A and also found in B? (matched — operating normally)
    (4) Which branches are in A but have NO movements in B? (official but inactive in system)
    (5) Which branches appear in B but NOT in A? (movement data from unlisted branches)
  • This is a data quality check — explain what each discrepancy means operationally.

═══════════════════════════════════════════════
OUTPUT FORMAT — STRICT
═══════════════════════════════════════════════
Return valid JSON with these exact keys:
{
  "answer": "Rich analytical narrative — NO raw JSON inside this string",
  "decision_needed": true/false,
  "decision_card": null OR {
    "question": "What decision needs to be made?",
    "recommendation": "What we recommend",
    "rationale": "Why",
    "options": [
      {"label": "A", "action": "...", "benefit": "...", "risk": "..."},
      {"label": "B", "action": "...", "benefit": "...", "risk": "..."}
    ]
  },
  "suggested_followups": [
    "Specific follow-up question 1",
    "Specific follow-up question 2",
    "Specific follow-up question 3"
  ],
  "urgency": "low | medium | high | critical",
  "topic": "sales | inventory | aging | customers | purchases | analytical | data_quality | terminology",
  "key_metrics": {"metric_name": "formatted_value"}
}

Answer in the SAME LANGUAGE as the question (French, Arabic, or English).
The 'answer' field must be a plain readable string — no JSON objects, no code blocks inside it."""

    # ══════════════════════════════════════════════════════════════════════════
    # USER PROMPT
    # ══════════════════════════════════════════════════════════════════════════

    def _user_prompt(self, question: str, business_context: str, retrieval: dict) -> str:
        mode         = retrieval.get("mode", "sql")
        data_section = self._format(retrieval)

        instructions = (
            "ANALYTICAL TASK:\n"
            "1. Identify the key numbers in the data relevant to this question.\n"
            "2. Apply all 4 reasoning layers: Fact → Context → Implication → Action.\n"
            "3. Cross-reference datasets if multiple are available.\n"
            "4. Benchmark every figure (% of total, trend, comparison).\n"
            "5. For NO_DATA: diagnose WHY (period, import issue, data cutoff) — never just say 'no data'.\n"
            "6. For April 2026 showing 0: data goes to 2026-03-23 — this is a cutoff, NOT a revenue drop.\n"
            "   Never describe a data cutoff as 'une chute de X%' — that is factually wrong.\n"
            "7. RULE 9: Percentages > 100% for proportions are impossible — always explain line vs SKU counting.\n"
            "8. RULE 10: If aging total > 0 but all buckets = 0, diagnose the import bug explicitly.\n"
            "9. RULE 11: 'اعمار الدمم' = Arabic accounting term for aging of receivables, not Dammam city.\n"
            "10. RULE 12: For branch cross-reference questions, use the 5-point structure.\n"
            "11. Make the user smarter — not just inform them of a number."
        )

        return "\n\n".join([
            f"QUESTION:\n{question}",
            f"BUSINESS CONTEXT:\n{business_context or 'Libyan technology distributor — B2B sales, multiple branches.'}",
            f"DATA [{mode.upper()}]:\n{data_section}",
            instructions,
        ])

    # ══════════════════════════════════════════════════════════════════════════
    # ROUTEUR DE FORMATAGE
    # ══════════════════════════════════════════════════════════════════════════

    def _format(self, ctx: dict) -> str:
        if ctx.get("no_data"):
            return (
                f"NO_DATA: {ctx.get('no_data_message', 'Aucune donnée.')}\n"
                f"Available data range: check import logs."
            )

        mode = ctx.get("mode", "sql")
        
        if mode == "text_to_sql":
            return self._fmt_text_to_sql(ctx)

        if mode == "hybrid":
            return (
                f"[SQL]\n{self._fmt_sql(ctx.get('sql_context', {}))}\n\n"
                f"[SEMANTIC]\n{self._fmt_vector(ctx.get('vector_context', {}))}"
            )
        if mode == "vector":                  return self._fmt_vector(ctx)
        if mode == "llm_only":                return ctx.get("business_summary", "Répondre avec expertise métier.")
        if mode == "aging":                   return self._fmt_aging(ctx)
        if mode == "inventory":               return self._fmt_inventory(ctx)
        if mode == "analytical":              return self._fmt_analytical(ctx)
        if mode == "branch_movement_cross":   return self._fmt_branch_cross(ctx)
        if mode == "naming_explanation":      return self._fmt_naming_explanation(ctx)
        if mode == "customer_inactive_debt":  return self._fmt_customer_inactive_debt(ctx)
        return self._fmt_sql(ctx)

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE SQL
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_sql(self, ctx: dict) -> str:
        if not ctx:
            return "NO_DATA: Aucune donnée SQL."

        lines = []

        if ctx.get("summary"):
            s = ctx["summary"]
            rev  = s.get("total_revenue", 0)
            trans = s.get("transactions", 0)
            lines += [
                f"Period: {s.get('start_date')} → {s.get('end_date')}",
                f"Revenue:      {rev:>15,.2f} LYD",
                f"Qty sold:     {s.get('total_qty', 0):>15,.0f} units",
                f"Transactions: {trans:>15,}",
                f"Avg ticket:   {s.get('avg_ticket', rev/trans if trans else 0):>15,.2f} LYD",
            ]
            if ctx.get("monthly_breakdown") or ctx.get("monthly_sales"):
                months = ctx.get("monthly_breakdown") or ctx.get("monthly_sales", [])
                if months:
                    lines.append("Monthly trend:")
                    prev = None
                    for mo in months:
                        r = mo.get("total_revenue", mo.get("value", 0))
                        arrow = ""
                        if prev is not None and prev > 0:
                            pct = (r - prev) / prev * 100
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
                f"Sales:     {bo.get('sales', {}).get('total_revenue', 0):,.2f} LYD | "
                f"qty={bo.get('sales', {}).get('total_qty', 0):,.0f}",
                f"Purchases: {bo.get('purchases', {}).get('total_value', 0):,.2f} LYD",
                f"Returns:   {bo.get('returns', {}).get('total_value', 0):,.2f} LYD",
            ]
            if bo.get("inventory"):
                inv = bo["inventory"]
                sku   = inv.get("sku_count", 1) or 1
                zsk   = inv.get("zero_stock_count", 0)
                zpct  = round(zsk / sku * 100, 1)
                lines.append(
                    f"Stock:     {inv.get('total_value', 0):,.2f} LYD | "
                    f"SKUs={sku} | "
                    f"zero_stock_SKUs={zsk} ({zpct}%)"
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
                f"  Sales:   {s.get('total_revenue', 0):,.2f} LYD | qty={s.get('total_qty', 0):,.0f} | trans={s.get('transactions', 0)}",
                f"  Returns: {r.get('total_value', 0):,.2f} LYD",
            ]
            if cd.get("top_products"):
                lines.append(f"  Top products: {', '.join(p['material_name'] for p in cd['top_products'][:3])}")

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
                f"  Sales:     {s.get('total_revenue', 0):,.2f} LYD | qty={s.get('total_qty', 0):,.0f} | avg={s.get('avg_price', 0):,.2f}",
                f"  Purchases: {a.get('total_cost', 0):,.2f} LYD | qty={a.get('total_qty', 0):,.0f}",
                f"  Margin:    {pd.get('gross_margin', 0):,.2f} LYD ({pd.get('gross_margin_pct', 0):.1f}%)",
            ]
            if pd.get("by_branch"):
                lines.append("  By branch (sales):")
                for b in pd["by_branch"][:5]:
                    lines.append(
                        f"    • {b.get('branch_name', 'N/A')}: "
                        f"{b.get('total_revenue', 0):,.2f} LYD | qty={b.get('total_qty', 0):,.0f}"
                    )
            if pd.get("current_stock") and pd["current_stock"].get("by_branch"):
                lines.append("  Current stock by branch:")
                for b in pd["current_stock"]["by_branch"]:
                    status = "✓" if b.get("quantity", 0) > 0 else "✗ OUT OF STOCK"
                    lines.append(
                        f"    {status} {b.get('branch_name', 'N/A')}: "
                        f"qty={b.get('quantity', 0):,.0f} | value={b.get('line_value', 0):,.2f} LYD"
                    )

        if ctx.get("top_products"):
            lines.append("Top products by revenue:")
            for i, p in enumerate(ctx["top_products"][:10], 1):
                lines.append(
                    f"  {i}. [{p.get('material_code', '')}] {p.get('material_name', 'N/A')}: "
                    f"{p.get('total_revenue', 0):,.2f} LYD | qty={p.get('total_qty', 0):,.0f}"
                )

        if ctx.get("purchases_summary"):
            ps = ctx["purchases_summary"]
            lines += [
                f"Purchases — Period: {ps.get('start_date')} → {ps.get('end_date')}",
                f"  Total: {ps.get('total_value', 0):,.2f} LYD | qty={ps.get('total_qty', 0):,.0f} | trans={ps.get('transactions', 0)}",
            ]
        if ctx.get("top_suppliers"):
            lines.append("Top suppliers:")
            for i, s in enumerate(ctx["top_suppliers"][:5], 1):
                lines.append(
                    f"  {i}. {s.get('supplier_name', 'N/A')}: "
                    f"{s.get('total_value', 0):,.2f} LYD | qty={s.get('total_qty', 0):,.0f} | trans={s.get('transactions', 0)}"
                )
        if ctx.get("supplier_detail"):
            sd = ctx["supplier_detail"]
            lines += [
                f"Supplier detail — {sd.get('supplier_name', 'N/A')}:",
                f"  Purchases: {sd.get('purchases', {}).get('total_value', 0):,.2f} LYD",
                f"  Returns:   {sd.get('returns', {}).get('total_value', 0):,.2f} LYD",
            ]

        if ctx.get("returns_sale"):
            rs = ctx["returns_sale"]
            lines.append(
                f"Customer returns (مردودات بيع): {rs.get('total_value', 0):,.2f} LYD | "
                f"qty={rs.get('total_qty', 0):,.0f} | trans={rs.get('transactions', 0)}"
            )
        if ctx.get("returns_buy"):
            rb = ctx["returns_buy"]
            lines.append(
                f"Supplier returns (مردود شراء): {rb.get('total_value', 0):,.2f} LYD | "
                f"qty={rb.get('total_qty', 0):,.0f} | trans={rb.get('transactions', 0)}"
            )

        if ctx.get("transfers"):
            tf = ctx["transfers"]
            lines.append(
                f"Transfers (نقل): "
                f"in={tf.get('transfers_in', {}).get('value', 0):,.2f} LYD | "
                f"out={tf.get('transfers_out', {}).get('value', 0):,.2f} LYD | "
                f"trans={tf.get('transfers_in', {}).get('transactions', 0)}"
            )

        if ctx.get("damaged"):
            d = ctx["damaged"]
            lines.append(
                f"Damaged (ف.تالف): {d.get('total_value', 0):,.2f} LYD | "
                f"qty={d.get('total_qty', 0):,.0f} | trans={d.get('transactions', 0)}"
            )
            if d.get("items"):
                lines.append("Damaged items detail:")
                for item in d["items"][:5]:
                    lines.append(
                        f"  • {item.get('product', 'N/A')} @ {item.get('branch', '')} "
                        f"({item.get('date', '')}): qty={item.get('qty', 0):,.0f} | {item.get('value', 0):,.2f} LYD"
                    )

        if ctx.get("opening_stock"):
            op = ctx["opening_stock"]
            lines.append(
                f"Opening stock (ف.أول المدة): {op.get('total_value', 0):,.2f} LYD | "
                f"qty={op.get('total_qty', 0):,.0f} | trans={op.get('transactions', 0)}"
            )
            if op.get("filter_product"):
                lines.append(f"  Product filter: {op['filter_product']}")
            if op.get("by_branch"):
                for b in op["by_branch"]:
                    lines.append(
                        f"  • {b.get('branch_name', 'N/A')}: "
                        f"qty={b.get('total_qty', 0):,.0f} | {b.get('total_value', 0):,.2f} LYD"
                    )

        if ctx.get("margin"):
            mg = ctx["margin"]
            lines += [
                f"Gross margin — Period: {mg.get('start_date')} → {mg.get('end_date')}",
                f"  Net revenue: {mg.get('net_revenue', 0):,.2f} LYD",
                f"  Net cost:    {mg.get('net_cost', 0):,.2f} LYD",
                f"  Gross margin: {mg.get('gross_margin', 0):,.2f} LYD ({mg.get('gross_margin_pct', 0):.1f}%)",
                f"  Returns revenue: {mg.get('returns_revenue', 0):,.2f} | Returns cost: {mg.get('returns_cost', 0):,.2f}",
            ]

        if ctx.get("monthly_sales"):
            lines.append("Monthly sales:")
            prev = None
            for mo in ctx["monthly_sales"]:
                rev = mo.get("total_revenue", 0)
                arrow = ""
                if prev is not None and prev > 0:
                    pct = (rev - prev) / prev * 100
                    arrow = f" ({'+' if pct >= 0 else ''}{pct:.0f}%)"
                lines.append(f"  {mo.get('month', '?')}: {rev:,.2f} LYD | trans={mo.get('transactions', 0)}{arrow}")
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
                f"out={adj.get('adjustments_out', {}).get('value', 0):,.2f} LYD | "
                f"trans={adj.get('transactions', 0)}"
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
    # FORMATAGE AGING — FIX-2 : bug d'import toujours explicite
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

        # FIX-2 : Détecter le bug d'import EXPLICITEMENT et l'exposer dans les données
        # has_import_bug vient maintenant du sql_service (calculé à la source)
        has_import_bug = ctx.get("has_import_bug", False)
        if not has_import_bug:
            bucket_sum = sum(ag.get(k, 0) for k in ["d1_30", "d31_60", "d61_90", "d91_120",
                                                      "d121_150", "d151_180", "over_180", "current"])
            has_import_bug = (total > 0 and bucket_sum == 0)

        lines = [
            f"Report date: {ctx.get('report_date', 'N/A')}",
            f"Total receivables: {total:,.2f} LYD",
        ]

        if has_import_bug:
            # Bug d'import : on montre le total réel mais on avertit clairement
            lines += [
                "",
                "⚠ IMPORT COLUMN-MAPPING BUG DETECTED:",
                f"  Total receivable = {total:,.2f} LYD — this figure IS reliable (summed directly).",
                "  BUT all aging bucket fields (current / 1-30d / 31-60d / 61-90d / 91-120d ...)",
                "  are showing 0 LYD — this is because the Excel column order in the uploaded file",
                "  does not match the parser's expected column positions.",
                "  CONSEQUENCE: the overdue breakdown below CANNOT be trusted.",
                "  ACTION REQUIRED: re-import اعمار__الدمم__2026.xlsx after verifying column order.",
                "",
                "DATA THAT CAN BE USED DESPITE THE BUG:",
            ]
        else:
            lines += [
                f"Current (not due):  {current:,.2f} LYD"
                + (f" ({current/total*100:.1f}%)" if total else ""),
                f"OVERDUE TOTAL:      {overdue:,.2f} LYD ({ag.get('pct_overdue', 0):.1f}%)",
                "",
                "Aging bucket breakdown:",
                f"  1–30 days:    {ag.get('d1_30',    0):>12,.2f} LYD",
                f"  31–60 days:   {ag.get('d31_60',   0):>12,.2f} LYD",
                f"  61–90 days:   {ag.get('d61_90',   0):>12,.2f} LYD",
                f"  91–120 days:  {ag.get('d91_120',  0):>12,.2f} LYD  ← COLLECTION URGENT",
                f"  121–150 days: {ag.get('d121_150', 0):>12,.2f} LYD",
                f"  151–180 days: {ag.get('d151_180', 0):>12,.2f} LYD",
                f"  >180 days:    {ag.get('over_180', 0):>12,.2f} LYD  ← WRITE-OFF RISK",
            ]

        lines.append(f"  Total accounts: {ag.get('account_count', 0)}")

        if ctx.get("customers_stats"):
            cs = ctx["customers_stats"]
            lines.append(
                f"\nCustomers context: total={cs.get('total',0)} | "
                f"active={cs.get('active',0)} | inactive={cs.get('inactive',0)}"
            )

        if ctx.get("critical_accounts") and not has_import_bug:
            lines.append("\nCritical accounts (>90d overdue):")
            for acc in ctx["critical_accounts"][:8]:
                lines.append(
                    f"  ⚠ {acc.get('account', 'N/A')}: "
                    f"total={acc.get('total', 0):,.2f} LYD | "
                    f"overdue_90d={acc.get('overdue_90', 0):,.2f} LYD | "
                    f"risk={acc.get('risk_score', 'N/A')}"
                )

        if ctx.get("top_accounts"):
            lines.append("\nTop accounts by total exposure:")
            for acc in ctx["top_accounts"][:5]:
                if has_import_bug:
                    # Si bug d'import, montrer uniquement le total (seule valeur fiable)
                    lines.append(
                        f"  • {acc.get('account', 'N/A')}: "
                        f"total={acc.get('total', 0):,.2f} LYD "
                        f"(bucket breakdown unavailable — import bug)"
                    )
                else:
                    lines.append(
                        f"  • {acc.get('account', 'N/A')}: "
                        f"total={acc.get('total', 0):,.2f} LYD | "
                        f"current={acc.get('current', 0):,.2f} | "
                        f"1-30d={acc.get('d1_30', 0):,.2f} | "
                        f"31-60d={acc.get('d31_60', 0):,.2f} | "
                        f"61-90d={acc.get('d61_90', 0):,.2f} | "
                        f"91-120d={acc.get('d91_120', 0):,.2f}"
                    )

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE INVENTAIRE — FIX-3 : % toujours ≤ 100%
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_inventory(self, ctx: dict) -> str:
        if ctx.get("no_data"):
            return f"NO_DATA: {ctx.get('no_data_message', 'Aucune donnée stock.')}"

        if not ctx or not ctx.get("summary"):
            err = ctx.get("error", "") if ctx else ""
            return (
                "NO_DATA: InventorySnapshotLine est vide.\n"
                "DIAGNOSTIC: Le fichier جرد__افقي__2026.xlsx n'a pas encore été importé. "
                + (f"Error: {err}" if err else "")
            )

        inv = ctx["summary"]
        lines = []

        if ctx.get("filter_branch"):
            lines.append(f"Filter: branch = {ctx['filter_branch']}")
        if ctx.get("filter_product"):
            lines.append(f"Filter: product = {ctx['filter_product']}")

        sku_count         = inv.get("sku_count", 0) or 1
        total_lines       = inv.get("total_lines", 0)
        zero_stock_skus   = inv.get("zero_stock_count", 0)    # SKUs uniques — toujours ≤ sku_count
        zero_stock_lines  = inv.get("zero_stock_lines", 0)    # lignes brutes (peut > sku_count)
        zero_pct          = round(zero_stock_skus / sku_count * 100, 1)

        lines += [
            f"Total unique SKUs:        {sku_count:,}",
            f"Total inventory lines:    {total_lines:,} (1 line per SKU per branch)",
            f"Total quantity:           {inv.get('total_qty', 0):,.0f} units",
            f"Total value:              {inv.get('total_value', 0):,.2f} LYD",
            f"Zero-stock SKUs:          {zero_stock_skus:,} ({zero_pct:.1f}% of SKUs)",
            f"Zero-stock lines (raw):   {zero_stock_lines:,}",
            "NOTE: zero-stock lines > unique SKUs is expected (each SKU × N branches).",
        ]

        if ctx.get("filter_branch") and sku_count == 0:
            lines.append(
                f"\n⚠ Branch '{ctx['filter_branch']}' not found in inventory. "
                "Check branch name spelling."
            )

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

        if ctx.get("top_by_value"):
            lines.append("\nTop products by value:")
            for p in ctx["top_by_value"][:5]:
                lines.append(
                    f"  - [{p.get('product_code', '')}] {p.get('product_name', 'N/A')} "
                    f"@ {p.get('branch_name', '')}: "
                    f"qty={p.get('total_qty', 0):,.0f} | {p.get('total_value', 0):,.2f} LYD"
                )

        if ctx.get("out_of_stock"):
            lines.append(f"\nOut-of-stock items (showing {len(ctx['out_of_stock'])}):")
            for p in ctx["out_of_stock"][:8]:
                lines.append(
                    f"  ✗ [{p.get('product_code', '')}] {p.get('product_name', 'N/A')} "
                    f"@ {p.get('branch_name', '')}"
                )

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE CROISEMENT BRANCHES ↔ MOUVEMENTS [FIX-4 NOUVEAU]
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_branch_cross(self, ctx: dict) -> str:
        cross = ctx.get("cross", {})
        mv    = ctx.get("movements_summary", {})
        lines = [
            f"=== BRANCH CROSS-REFERENCE (RULE 12) ===",
            f"Period analysed: {ctx.get('period', 'N/A')}",
            "",
            f"Official branches (from فروع_بروتكتا model): {cross.get('official_count', 0)}",
            f"Distinct branches found in movements (حركة_المادة): {cross.get('movement_count', 0)}",
            "",
            "MATCHED — in both official list AND movements:",
        ]
        for b in cross.get("matched", []):
            lines.append(f"  ✓ {b}")
        if not cross.get("matched"):
            lines.append("  (none)")

        lines.append("\nIN OFFICIAL LIST but NO movements found:")
        for b in cross.get("in_official_not_movement", []):
            lines.append(f"  ✗ {b}  ← no transactions recorded for this branch")
        if not cross.get("in_official_not_movement"):
            lines.append("  (none — all official branches have movements)")

        lines.append("\nIN MOVEMENTS but NOT in official branch list:")
        for b in cross.get("in_movement_not_official", []):
            lines.append(f"  ? {b}  ← transactions exist but branch not in official file")
        if not cross.get("in_movement_not_official"):
            lines.append("  (none — no ghost branches detected)")

        if mv:
            total_sales = mv.get("sales", {}).get("value", 0)
            lines += [
                "",
                f"Movement totals for period:",
                f"  Sales (ف بيع):       {total_sales:,.2f} LYD",
                f"  Purchases (ف شراء):  {mv.get('purchases', {}).get('value', 0):,.2f} LYD",
                f"  Transfers (نقل):     {mv.get('transfers_in', {}).get('value', 0):,.2f} LYD",
            ]

        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE EXPLICATION NOMENCLATURE [FIX-5 NOUVEAU]
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_naming_explanation(self, ctx: dict) -> str:
        term = ctx.get("term", "")
        lines = [
            "=== ARABIC ACCOUNTING TERMINOLOGY CONTEXT ===",
            "",
            "Term in question: اعمار_الدمم (or اعمار الديون / أعمار الذمم)",
            "",
            "ETYMOLOGY:",
            "  Root: الذِمَّة (al-dhimma) = financial obligation, liability, trust",
            "  Plural: الذِمَم or الدِّمَم (both correct in Classical Arabic)",
            "  Compound: أعمار الذمم المدينة = aging of accounts receivable",
            "  Common ERP abbreviation: اعمار الدمم (used in Libyan/Gulf Arabic ERP)",
            "",
            "IMPORTANT DISTINCTION:",
            "  الدمام (al-Dammam) = Saudi Arabian city — written with long ā: دَامَّ",
            "  الدِمَم (al-dimam) = debts/obligations — written with kasra: دِمَم",
            "  These are DIFFERENT words — different vowels, different meaning.",
            "",
            "CONCLUSION:",
            "  The file اعمار__الدمم__2026.xlsx is an ACCOUNTS RECEIVABLE AGING report,",
            "  following standard Arabic bookkeeping terminology used across Libya, Tunisia,",
            "  Algeria and Gulf states in ERP systems (Odoo, Epicor, local AS400 derivatives).",
            "  It has NO connection to the city of Dammam.",
            "",
            f"Business context: {ctx.get('business_context', '')}",
        ]
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE CROISEMENT CLIENT INACTIF + DETTE [FIX-6 NOUVEAU]
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_customer_inactive_debt(self, ctx: dict) -> str:
        no_sales = ctx.get("no_sales_customers", [])
        aging    = ctx.get("aging_data", {})
        period   = ctx.get("period", "N/A")
        cs       = ctx.get("customers_stats", {})

        ag_sum   = aging.get("aging_summary", {}) or {}
        total_debt = ag_sum.get("total", 0)
        has_bug    = aging.get("has_import_bug", False)

        lines = [
            "=== CLIENTS WITH DEBT BUT NO RECENT SALES ===",
            f"Period checked for sales: {period}",
            f"Total receivables: {total_debt:,.2f} LYD",
            f"Customers: total={cs.get('total',0)} | active={cs.get('active',0)}",
            "",
            "METHODOLOGY NOTE:",
            "  Matching is done on customer_name (movements) vs account (aging).",
            "  If naming conventions differ between files, some matches may be missed.",
            "  A definitive match requires a shared account_code key.",
            "",
        ]

        if has_bug:
            lines.append(
                "⚠ Aging import bug active — individual bucket amounts unreliable.\n"
                "  Only total debt per account is reliable.\n"
            )

        if not no_sales:
            lines.append(
                "NO_DATA: Could not identify customers with debt and no sales.\n"
                "REASON: The aging file uses account names (e.g. 'موزع/حسن السعيطي')\n"
                "while the movements file uses customer_name field.\n"
                "These may not match exactly — a direct SQL join on account_code would be needed.\n"
                "RECOMMENDED ACTION: Add account_code to both Customer and AgingReceivable models\n"
                "and join on that field instead of name."
            )
        else:
            lines.append("Results (sorted: no-sales-first, then by debt):")
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

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE ANALYTIQUE
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_analytical(self, ctx: dict) -> str:
        sections = ctx.get("sections", {})
        parts    = []

        if "sales" in sections:
            s  = sections["sales"]
            sm = s.get("summary", {})
            rev  = sm.get("total_revenue", 0)
            trans = sm.get("transactions", 0)
            block = [
                f"[SALES] Period: {sm.get('start_date')} → {sm.get('end_date')}",
                f"  Revenue:      {rev:,.2f} LYD",
                f"  Transactions: {trans:,}",
                f"  Avg ticket:   {rev/trans:,.2f} LYD" if trans else "",
            ]
            if s.get("monthly"):
                block.append("  Monthly trend:")
                prev = None
                for mo in s["monthly"]:
                    r = mo.get("total_revenue", 0)
                    arrow = ""
                    if prev is not None and prev > 0:
                        pct = (r - prev) / prev * 100
                        arrow = f" ({'+' if pct >= 0 else ''}{pct:.0f}%)"
                    block.append(f"    {mo['month']}: {r:,.2f} LYD{arrow}")
                    prev = r
            if s.get("top_customers"):
                block.append("  Top customers:")
                for c in s["top_customers"][:3]:
                    pct = c.get("total_revenue", 0) / rev * 100 if rev else 0
                    block.append(
                        f"    {c['customer_name']}: "
                        f"{c.get('total_revenue', 0):,.2f} LYD ({pct:.1f}% of revenue)"
                    )
            if s.get("top_branches"):
                block.append("  Top branches:")
                for b in s["top_branches"][:3]:
                    pct = b.get("total_revenue", 0) / rev * 100 if rev else 0
                    block.append(
                        f"    {b.get('branch_name', 'N/A')}: "
                        f"{b.get('total_revenue', 0):,.2f} LYD ({pct:.1f}%)"
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
                f"  Net revenue: {mg.get('net_revenue', 0):,.2f} LYD\n"
                f"  Net cost:    {mg.get('net_cost', 0):,.2f} LYD\n"
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
                f"  Customer (مردودات بيع): {rs.get('total_value', 0):,.2f} LYD | "
                f"qty={rs.get('total_qty', 0):,.0f} | trans={rs.get('transactions', 0)}\n"
                f"  Supplier (مردود شراء):  {rb.get('total_value', 0):,.2f} LYD | "
                f"qty={rb.get('total_qty', 0):,.0f} | trans={rb.get('transactions', 0)}"
            )

        if "customers" in sections:
            c = sections["customers"]
            parts.append(
                f"[CUSTOMERS] total={c.get('total', 0)} | "
                f"active={c.get('active', 0)} | inactive={c.get('inactive', 0)}"
            )

        if "damaged" in sections:
            d = sections["damaged"]
            parts.append(
                f"[DAMAGED (ف.تالف)] {d.get('total_value', 0):,.2f} LYD | "
                f"qty={d.get('total_qty', 0):,.0f} | trans={d.get('transactions', 0)}"
            )

        return "\n\n".join(parts) if parts else "NO_DATA: Aucune donnée analytique."

    # ══════════════════════════════════════════════════════════════════════════
    # FORMATAGE VECTOR
    # ══════════════════════════════════════════════════════════════════════════

    def _fmt_vector(self, ctx: dict) -> str:
        items = ctx.get("items") or []
        if not items:
            return "NO_DATA: Aucun résultat sémantique — Qdrant peut être vide ou non configuré."
        return "\n".join(
            f"[{i+1}] {item.get('text', '')[:400]} (score={item.get('score', 0):.3f})"
            for i, item in enumerate(items[:6])
        )
        
    def _fmt_text_to_sql(self, ctx: dict) -> str:
        """Formate le résultat Text-to-SQL pour le prompt RAG."""
        t2s = ctx.get("text_to_sql", {})

        if not t2s.get("success"):
            return f"TEXT-TO-SQL FAILED: {t2s.get('error', 'unknown')}"

        lines = [
            "=== RÉSULTAT TEXT-TO-SQL ===",
            f"Requête interprétée : {t2s.get('explanation', '')}",
            f"Confiance : {t2s.get('confidence', '?')}",
            f"Lignes : {t2s.get('row_count', 0)}",
            "",
            ctx.get("prompt_context", ""),
        ]

        return "\n".join(lines)