"""
apps/ai_insights/chat_views.py — BusinessContextBuilder enrichi
===============================================================
Modifications :
  - _add_aging_detail_context   : top débiteurs avec buckets d'ancienneté
  - _add_inventory_context      : stock par branche, ruptures, valeur totale
  - _add_customers_context      : top clients actifs, codes comptes
  - _add_branches_context       : liste des succursales actives
  - _add_movements_context      : TOUS les types de mouvements (pas seulement ventes)
  - _add_sales_context          : conservé pour compatibilité (MTD/YTD live)
"""

import json
import logging
from datetime import date

from django.core.cache import cache
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AIConversation, AIConversationMessage
from .serializers import (
    AIConversationCreateSerializer,
    AIConversationMessageSerializer,
    AIConversationSerializer,
)
from .services.memory_service import MemoryService

logger = logging.getLogger(__name__)


def _cache_key(prefix: str, company_id: str, **kwargs) -> str:
    suffix = ":".join(f"{k}{v}" for k, v in sorted(kwargs.items()))
    return f"ai:{prefix}:{company_id}:{suffix}"


def _require_company(request):
    company = getattr(request.user, "company", None)
    if not company:
        return None, Response(
            {"error": "Your account is not linked to a company."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return company, None


def _conversation_title_from_text(text: str) -> str:
    cleaned = (text or "").strip().replace("\n", " ")
    if not cleaned:
        return "Decision Advisor session"
    return cleaned[:120]


# ── Conversations list/create ─────────────────────────────────────────────────

class AIConversationListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company, err = _require_company(request)
        if err:
            return err
        conversations = (
            AIConversation.objects
            .filter(company=company, user=request.user)
            .order_by("-updated_at")[:50]
        )
        data = AIConversationSerializer(conversations, many=True).data
        return Response({"count": len(data), "conversations": data})

    def post(self, request):
        company, err = _require_company(request)
        if err:
            return err
        serializer = AIConversationCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        conversation = AIConversation.objects.create(
            company=company,
            user=request.user,
            title=(serializer.validated_data.get("title") or "").strip()[:255],
        )
        return Response(
            AIConversationSerializer(conversation).data,
            status=status.HTTP_201_CREATED,
        )


class AIConversationMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        company, err = _require_company(request)
        if err:
            return err
        conversation = (
            AIConversation.objects
            .filter(id=conversation_id, company=company, user=request.user)
            .first()
        )
        if not conversation:
            return Response(
                {"error": "Conversation not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        messages = conversation.messages.order_by("created_at")
        data = AIConversationMessageSerializer(messages, many=True).data
        return Response({
            "conversation": AIConversationSerializer(conversation).data,
            "count": len(data),
            "messages": data,
        })


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are WEEG Decision Advisor — a senior business analyst embedded in a BI \
platform for Libyan distribution companies. You act as a trusted advisor to \
the company manager, helping them make concrete, data-backed decisions.

Today is {today}. Currency: LYD (Libyan Dinar).

=== LIVE BUSINESS CONTEXT ===
{context}
=============================

YOUR ROLE:
You are not a chatbot — you are a decision advisor. When the manager asks a \
question, you:
  1. Answer directly with exact numbers from the context.
  2. Identify the DECISION the manager needs to make (if any).
  3. Give a clear recommendation: what to do, who should act, by when.
  4. Anticipate the next question — suggest 2-3 relevant follow-up questions.
  5. If a decision has trade-offs, present them concisely (Pros / Cons).

RESPONSE FORMAT — always return valid JSON:
{{
  "answer": "<your main response — 2-5 sentences, specific numbers, direct>",
  "decision_needed": true | false,
  "decision_card": {{
    "question": "<the key decision to make>",
    "recommendation": "<your clear recommendation>",
    "rationale": "<why — 1 sentence with data>",
    "options": [
      {{"label": "<option A>", "pros": "<benefit>", "cons": "<risk>"}},
      {{"label": "<option B>", "pros": "<benefit>", "cons": "<risk>"}}
    ],
    "owner": "<who should act>",
    "deadline": "<by when>"
  }},
  "suggested_followups": [
    "<question 1>",
    "<question 2>",
    "<question 3>"
  ],
  "urgency": "critical" | "high" | "medium" | "low",
  "topic": "credit" | "stock" | "churn" | "forecast" | "revenue" | "general"
}}

If no decision is needed (factual question), set decision_card to null.
Always include 2-3 suggested_followups relevant to the manager's concern.
Respond in {language}.
"""


# ── Context builder ───────────────────────────────────────────────────────────

class BusinessContextBuilder:
    """
    Construit un contexte textuel riche depuis toutes les sources de données.
    Couvre : ventes, créances, stock, clients, succursales, mouvements, AI caches.
    """

    def build(self, company, user_role: str = "manager", companies: list = None) -> str:
        lines = []

        if companies and len(companies) > 1:
            lines.append(
                f"[COMPANIES] User has access to {len(companies)} companies: "
                + ", ".join(c.name for c in companies[:10])
            )

        # ── Sources de données directes (DB) ──────────────────────────────────
        self._add_sales_context(company, lines)
        self._add_movements_context(company, lines)
        self._add_aging_detail_context(company, lines)
        self._add_inventory_context(company, lines)
        self._add_customers_context(company, lines)
        self._add_branches_context(company, lines)

        # ── Caches des analyzers AI ────────────────────────────────────────────
        self._add_critical_context(company, lines, user_role)
        self._add_churn_context(company, lines)
        self._add_stock_context(company, lines)
        self._add_forecast_context(company, lines)
        self._add_seasonal_context(company, lines)
        self._add_anomaly_context(company, lines)

        if not lines:
            lines.append(
                "No cached data — ask the manager to refresh the dashboards first."
            )
        return "\n".join(lines)

    # ── VENTES LIVE (MTD / YTD) ───────────────────────────────────────────────

    def _add_sales_context(self, company, lines):
        try:
            from apps.transactions.models import MaterialMovement
            from django.db.models import Sum, Count, Q

            today     = date.today()
            m_start   = today.replace(day=1)
            ytd_start = date(today.year, 1, 1)

            base = MaterialMovement.objects.filter(
                company=company, movement_type="ف بيع"
            )
            mtd = base.filter(movement_date__gte=m_start).aggregate(
                rev=Sum("total_out"), txns=Count("id")
            )
            ytd = base.filter(movement_date__gte=ytd_start).aggregate(
                rev=Sum("total_out")
            )
            mtd_rev  = float(mtd["rev"] or 0)
            ytd_rev  = float(ytd["rev"] or 0)
            mtd_txns = mtd["txns"] or 0

            lines.append(
                f"[SALES LIVE] MTD ({today.strftime('%b %Y')}): "
                f"{mtd_rev:,.0f} LYD ({mtd_txns} transactions) | "
                f"YTD: {ytd_rev:,.0f} LYD"
            )
            top = (
                base.filter(movement_date__gte=m_start)
                .exclude(Q(customer_name__isnull=True) | Q(customer_name=""))
                .values("customer_name")
                .annotate(rev=Sum("total_out"))
                .order_by("-rev")[:3]
            )
            for c in top:
                lines.append(
                    f"  · {c['customer_name']}: {float(c['rev']):,.0f} LYD this month"
                )
        except Exception as exc:
            logger.debug("[Chat] sales context failed: %s", exc)

    # ── TOUS LES MOUVEMENTS (pas seulement ventes) ────────────────────────────

    def _add_movements_context(self, company, lines):
        try:
            from apps.transactions.models import MaterialMovement
            from django.db.models import Sum, Count

            today     = date.today()
            m_start   = today.replace(day=1)

            # Résumé par type de mouvement ce mois
            by_type = (
                MaterialMovement.objects
                .filter(company=company, movement_date__gte=m_start)
                .exclude(movement_type__isnull=True)
                .values("movement_type")
                .annotate(
                    total_in=Sum("total_in"),
                    total_out=Sum("total_out"),
                    count=Count("id"),
                )
                .order_by("-count")[:8]
            )

            if not by_type:
                return

            lines.append(f"[MOVEMENTS MTD] Breakdown by type ({today.strftime('%b %Y')}):")
            for row in by_type:
                mv_type  = row["movement_type"] or "N/A"
                t_in     = float(row["total_in"] or 0)
                t_out    = float(row["total_out"] or 0)
                count    = row["count"] or 0
                lines.append(
                    f"  · {mv_type}: {count} transactions | "
                    f"IN={t_in:,.0f} LYD | OUT={t_out:,.0f} LYD"
                )
        except Exception as exc:
            logger.debug("[Chat] movements context failed: %s", exc)

    # ── CRÉANCES DÉTAILLÉES ────────────────────────────────────────────────────

    def _add_aging_detail_context(self, company, lines):
        try:
            from apps.aging.models import AgingReceivable, AgingSnapshot
            from django.db.models import Sum, Q
            from django.db.models.functions import Coalesce
            from decimal import Decimal

            snap = (
                AgingSnapshot.objects
                .filter(company=company)
                .order_by("-uploaded_at")
                .first()
            )
            if not snap:
                return

            qs = AgingReceivable.objects.filter(snapshot=snap)
            ag = qs.aggregate(
                total=Coalesce(Sum("total"), Decimal("0")),
                current=Coalesce(Sum("current"), Decimal("0")),
            )
            grand   = float(ag["total"])
            curr    = float(ag["current"])
            overdue = max(0, grand - curr)
            or_pct  = round(overdue / grand * 100, 1) if grand > 0 else 0

            lines.append(
                f"[RECEIVABLES] Total: {grand:,.0f} LYD | "
                f"Overdue: {overdue:,.0f} LYD ({or_pct}%) | "
                f"Snapshot: {snap.uploaded_at.date()} | Year: {snap.aging_year}"
            )

            # Top 5 débiteurs avec détail
            top_overdue = list(
                qs.filter(total__gt=0)
                .order_by("-total")
                .values("account", "account_code", "total", "current",
                        "d61_90", "d91_120", "over_330", "risk_score")[:5]
            )
            for r in top_overdue:
                rec_overdue = max(0, float(r["total"]) - float(r["current"]))
                if rec_overdue > 0:
                    lines.append(
                        f"  · {r['account'][:50]} ({r['account_code']}): "
                        f"{float(r['total']):,.0f} LYD total | "
                        f"{rec_overdue:,.0f} LYD overdue | "
                        f"Risk: {r['risk_score'] or 'unknown'}"
                    )
        except Exception as exc:
            logger.debug("[Chat] aging detail context failed: %s", exc)

    # ── INVENTAIRE ────────────────────────────────────────────────────────────

    def _add_inventory_context(self, company, lines):
        try:
            from apps.inventory.models import InventorySnapshotLine
            from django.db.models import Sum, Count
            from django.db.models.functions import Coalesce
            from decimal import Decimal

            inv_qs = InventorySnapshotLine.objects.filter(company=company)
            if not inv_qs.exists():
                return

            agg = inv_qs.aggregate(
                total_qty=Coalesce(Sum("quantity"), Decimal("0")),
                total_value=Coalesce(Sum("line_value"), Decimal("0")),
                total_products=Count("product_code", distinct=True),
            )

            zero_stock = inv_qs.filter(quantity=0).count()
            total_lines = inv_qs.count()

            lines.append(
                f"[INVENTORY] Products: {agg['total_products']} SKUs | "
                f"Total qty: {float(agg['total_qty']):,.0f} units | "
                f"Total value: {float(agg['total_value']):,.0f} LYD | "
                f"Zero stock: {zero_stock} SKUs"
            )

            # Top 5 produits par valeur
            top_products = (
                inv_qs
                .values("product_name", "product_code")
                .annotate(
                    total_qty=Coalesce(Sum("quantity"), Decimal("0")),
                    total_value=Coalesce(Sum("line_value"), Decimal("0")),
                )
                .order_by("-total_value")[:5]
            )
            for p in top_products:
                lines.append(
                    f"  · {(p['product_name'] or p['product_code'] or 'N/A')[:40]}: "
                    f"{float(p['total_qty']):,.0f} units | "
                    f"{float(p['total_value']):,.0f} LYD"
                )

            # Branches avec stock
            branches_with_stock = (
                inv_qs.filter(quantity__gt=0)
                .values("branch_name")
                .annotate(value=Coalesce(Sum("line_value"), Decimal("0")))
                .order_by("-value")[:5]
            )
            if branches_with_stock:
                branch_summary = " | ".join(
                    f"{b['branch_name']}: {float(b['value']):,.0f} LYD"
                    for b in branches_with_stock
                )
                lines.append(f"  Branches stock value: {branch_summary}")

        except Exception as exc:
            logger.debug("[Chat] inventory context failed: %s", exc)

    # ── CLIENTS ───────────────────────────────────────────────────────────────

    def _add_customers_context(self, company, lines):
        try:
            from apps.customers.models import Customer

            total_active = Customer.objects.filter(company=company, is_active=True).count()
            total_all    = Customer.objects.filter(company=company).count()

            if total_all == 0:
                return

            lines.append(
                f"[CUSTOMERS] Total: {total_all} | Active: {total_active} | "
                f"Inactive: {total_all - total_active}"
            )

            # Échantillon de clients récents
            recent = list(
                Customer.objects.filter(company=company, is_active=True)
                .order_by("-id")
                .values("name", "account_code", "area_code")[:5]
            )
            for c in recent:
                lines.append(
                    f"  · {c['name'][:40]} | Code: {c['account_code'] or 'N/A'} | "
                    f"Area: {c['area_code'] or 'N/A'}"
                )
        except Exception as exc:
            logger.debug("[Chat] customers context failed: %s", exc)

    # ── SUCCURSALES ───────────────────────────────────────────────────────────

    def _add_branches_context(self, company, lines):
        try:
            from apps.branches.models import Branch

            branches = list(
                Branch.objects.filter(is_active=True)
                .values("name", "address", "phone")[:10]
            )
            if not branches:
                return

            lines.append(f"[BRANCHES] Active branches: {len(branches)}")
            for b in branches:
                lines.append(
                    f"  · {b['name']} | {b['address'] or 'N/A'} | {b['phone'] or 'N/A'}"
                )
        except Exception as exc:
            logger.debug("[Chat] branches context failed: %s", exc)

    # ── CACHES ANALYZERS AI ───────────────────────────────────────────────────

    def _add_critical_context(self, company, lines, user_role):
        data = cache.get(_cache_key("critical", str(company.id), ai=1))
        if not data:
            return
        lines.append(
            f"[CRITICAL] Risk: {data.get('risk_level','?').upper()} | "
            f"{data.get('critical_count',0)} critical | "
            f"Exposure: {data.get('total_exposure_lyd',0):,.0f} LYD"
        )
        briefing = data.get("executive_briefing", "")
        if briefing:
            lines.append(f"  Summary: {briefing[:300]}")
        for s in (data.get("situations") or [])[:4]:
            name = (
                s.get("customer_name") or
                s.get("account_name") or
                s.get("product_name") or ""
            )
            name_part = f" — {name}" if name else ""
            lines.append(
                f"  · [{s['source'].upper()}]{name_part}: {s['title']} | "
                f"{s.get('financial_exposure_lyd',0):,.0f} LYD | "
                f"Act in {s.get('urgency_hours','?')}h"
            )
        for c in (data.get("causal_clusters") or [])[:2]:
            lines.append(
                f"  ⚡ CLUSTER: {c['cluster_name']} — "
                f"{c['common_cause'][:100]}"
            )

    def _add_churn_context(self, company, lines):
        data = cache.get(_cache_key("churn", str(company.id), n=20, ai=1))
        if not data:
            return
        s = data.get("summary", {})
        lines.append(
            f"[CHURN] Critical: {s.get('critical',0)} | "
            f"High: {s.get('high',0)} | "
            f"Avg score: {s.get('avg_churn_score',0)*100:.0f}%"
        )
        for p in (data.get("predictions") or [])[:6]:
            if p.get("churn_label") in ("critical", "high"):
                name = p.get("customer_name") or p.get("account_code") or "Unknown"
                lines.append(
                    f"  · {name}: score {p['churn_score']*100:.0f}% "
                    f"[{p['churn_label'].upper()}] | "
                    f"Inactive {p.get('days_since_last_purchase','?')}d | "
                    f"{p.get('avg_monthly_revenue_lyd',0):,.0f} LYD/mo"
                )

    def _add_stock_context(self, company, lines):
        data = cache.get(_cache_key("stock", str(company.id), ai=1))
        if not data:
            return
        s = data.get("summary", {})
        lines.append(
            f"[STOCK AI] Class A: {s.get('class_a_count',0)} SKUs | "
            f"Immediate reorders: {s.get('immediate_reorders',0)} | "
            f"Soon: {s.get('soon_reorders',0)}"
        )
        urgent = [
            i for i in (data.get("items") or [])
            if i.get("urgency") in ("immediate", "soon")
        ][:5]
        for item in urgent:
            days = item.get("estimated_days_to_stockout")
            lines.append(
                f"  · [{item['abc_class']}] {item['product_name'][:40]}: "
                f"stock={item['current_stock']:.0f} | "
                f"{'STOCKOUT' if not days else f'{days:.0f}d left'} | "
                f"EOQ={item['eoq']}"
            )

    def _add_forecast_context(self, company, lines):
        data = cache.get(_cache_key("predict", str(company.id), ai=1))
        if not data:
            return
        tm = data.get("trend_model", {})
        fc = data.get("revenue_forecast", [])
        lines.append(
            f"[FORECAST] Trend: {tm.get('direction','?')} "
            f"({(tm.get('slope_pct') or 0):+.2f}%/mo) | "
            f"MAPE: {tm.get('mape','-')}% | "
            f"3-mo base: {data.get('forecast_total_base_lyd',0):,.0f} LYD"
        )
        for m in fc[:3]:
            lines.append(
                f"  · {m['period']}: "
                f"expected {m.get('p50_lyd') or m['base_lyd']:,.0f} LYD"
            )

    def _add_seasonal_context(self, company, lines):
        data = cache.get(_cache_key("seasonal", str(company.id), ai=1))
        if not data or data.get("error"):
            return
        lines.append(
            f"[SEASONAL] Current: {data.get('current_season','?')} | "
            f"Peak months: {', '.join(data.get('peak_month_names',[]) or ['N/A'])} | "
            f"{'⚠ PEAK INCOMING' if data.get('upcoming_peak_alert') else 'No peak imminent'}"
        )

    def _add_anomaly_context(self, company, lines):
        data = cache.get(_cache_key("anomalies", str(company.id), ai=1))
        if not data:
            return
        s = data.get("summary", {})
        if s.get("total", 0) == 0:
            return
        lines.append(
            f"[ANOMALIES] {s.get('critical',0)} critical | "
            f"{s.get('high',0)} high — last 12 months"
        )
        for a in (data.get("anomalies") or [])[:3]:
            if a.get("severity") in ("critical", "high"):
                lines.append(
                    f"  · {a['date']} — {a['stream'].replace('_',' ')}: "
                    f"{a['direction']} {abs(a['deviation_pct']):.0f}% "
                    f"[{a['severity'].upper()}]"
                )


# ── Main chat view ────────────────────────────────────────────────────────────

class AIChatView(APIView):
    """
    POST /api/ai-insights/chat/

    Body:
    {
      "messages": [{"role": "user"|"assistant", "content": "..."}],
      "conversation_id": "<uuid optional>",
      "language": "en" | "fr" | "ar"
    }
    """
    permission_classes = [IsAuthenticated]

    MAX_HISTORY     = 30
    MAX_TOKENS      = 900
    MAX_CONTEXT_LEN = 4000

    def post(self, request):
        company, err = _require_company(request)
        if err:
            return err

        companies       = self._get_authorized_companies(request.user)
        messages        = request.data.get("messages", [])
        conversation_id = request.data.get("conversation_id")
        language        = request.data.get("language", "English")

        if not messages:
            return Response({"error": "messages is required."}, status=400)

        latest_user_message = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        if not latest_user_message:
            return Response({"error": "A user message is required."}, status=400)

        # ── Gérer la conversation ──────────────────────────────────────────────
        conversation = self._get_or_create_conversation(
            company, request.user, conversation_id, latest_user_message
        )
        if isinstance(conversation, Response):
            return conversation

        # ── Mémoire conversationnelle ──────────────────────────────────────────
        memory_service = MemoryService(str(conversation.id))
        memory_context = memory_service.get_context_for_prompt()

        # ── Contexte business (avec cache) ────────────────────────────────────
        context = self._get_business_context(company, request.user, companies)

        # ── LangGraph RAG Graph ───────────────────────────────────────────────
        try:
            from apps.ai_insights.services.langgraph_orchestrator import get_rag_graph

            graph = get_rag_graph()

            initial_state = {
                "question":             latest_user_message,
                "company_id":           company.id,
                "company":              company,
                "conversation_id":      str(conversation.id),
                "language":             language,
                "intent":               "",
                "complexity":           "ambiguous",
                "confidence":           0.0,
                "sql_context":          None,
                "vector_context":       None,
                "t2s_context":          None,
                "analyzer_context":     None,
                "conversation_summary": memory_context,
                "entity_memory":        memory_service.get_entity_memory(),
                "final_context":        "",
                "response":             None,
                "error":                None,
                "steps_taken":          [],
                "total_latency_ms":     0,
            }

            # Exécuter le graph
            final_state = graph.invoke(initial_state)
            reply = final_state.get("response")

            if reply and not reply.get("error"):
                # Persister le message et mettre à jour la mémoire
                self._persist_message(
                    conversation, request.user, latest_user_message, reply,
                    steps_taken=final_state.get("steps_taken", [])
                )
                memory_service.save_exchange(latest_user_message, reply)

                return Response({
                    "conversation_id": str(conversation.id),
                    **reply,
                    "fallback":     False,
                    "steps_taken":  final_state.get("steps_taken", []),
                })

        except Exception as exc:
            logger.error("[AIChatView] LangGraph failed: %s", exc, exc_info=True)

        # ── Fallback (inchangé) ────────────────────────────────────────────────
        fallback = self._build_fallback(latest_user_message, context)
        self._persist_message(conversation, request.user, latest_user_message, fallback)

        return Response({
            "conversation_id": str(conversation.id),
            **fallback,
            "fallback": True,
        })

    def _get_business_context(self, company, user, companies) -> str:
        """Contexte business avec cache court (5 min)."""
        cache_key = f"biz_ctx:{company.id}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        context = BusinessContextBuilder().build(
            company,
            user_role=getattr(user, "role", "manager") or "manager",
            companies=companies,
        )
        cache.set(cache_key, context, timeout=300)  # 5 min
        return context

    def _get_or_create_conversation(self, company, user, conversation_id, message):
        if conversation_id:
            conv = AIConversation.objects.filter(
                id=conversation_id, company=company, user=user
            ).first()
            if not conv:
                return Response({"error": "Conversation not found."}, status=404)
            return conv
        return AIConversation.objects.create(
            company=company,
            user=user,
            title=_conversation_title_from_text(message),
        )

    @staticmethod
    def _persist_message(conversation, user, user_msg, reply, steps_taken=None):
        """Persiste les messages avec transaction atomique."""
        from django.db import transaction
        with transaction.atomic():
            AIConversationMessage.objects.create(
                conversation=conversation,
                role=AIConversationMessage.Role.USER,
                content=user_msg,
                metadata={},
            )
            AIConversationMessage.objects.create(
                conversation=conversation,
                role=AIConversationMessage.Role.ASSISTANT,
                content=reply.get("answer", ""),
                metadata={
                    "decision_needed":     bool(reply.get("decision_needed", False)),
                    "decision_card":       reply.get("decision_card"),
                    "suggested_followups": reply.get("suggested_followups", []),
                    "urgency":             reply.get("urgency", "medium"),
                    "topic":               reply.get("topic", "general"),
                    "steps_taken":         steps_taken or [],
                },
            )
            conversation.save()
    @staticmethod
    def _get_authorized_companies(user) -> list:
        from apps.companies.models import Company
        try:
            if user.is_superuser or getattr(user, "is_staff", False):
                return list(Company.objects.all())
            if hasattr(user, "companies"):
                cos = list(user.companies.all())
                if cos:
                    return cos
        except Exception as exc:
            logger.debug("[AIChatView] _get_authorized_companies: %s", exc)
        if getattr(user, "company", None):
            return [user.company]
        return []

    def _call_ai(self, system_prompt: str, messages: list, company) -> dict | None:
        from django.conf import settings

        openai_key    = getattr(settings, "OPENAI_API_KEY", "").strip()

        if openai_key:
            try:
                import openai as _o
                client = _o.OpenAI(api_key=openai_key)
                model  = getattr(settings, "AI_MODEL_SMART", "gpt-4o-mini")
                msgs   = [{"role": "system", "content": system_prompt}] + messages
                resp   = client.chat.completions.create(
                    model=model,
                    max_tokens=self.MAX_TOKENS,
                    temperature=0.3,
                    messages=msgs,
                    response_format={"type": "json_object"},
                )
                raw = resp.choices[0].message.content if resp.choices else ""
                return self._parse_response(raw)
            except ImportError:
                logger.error("[AIChatView] openai package not installed")
            except Exception as exc:
                logger.error("[AIChatView] OpenAI failed company=%s: %s", company.id, exc)

        return None

    @staticmethod
    def _log_usage(company, usage) -> None:
        try:
            from apps.ai_insights.models import AIUsageLog
            tokens = (
                (getattr(usage, "input_tokens", 0) or 0) +
                (getattr(usage, "output_tokens", 0) or 0)
            )
            AIUsageLog.objects.create(
                analyzer="chat",
                model="decision_advisor",
                tokens_used=tokens,
                cost_usd=round(tokens / 1000 * 0.0003, 8),
                company=company,
            )
        except Exception:
            pass

    @staticmethod
    def _parse_response(raw: str) -> dict:
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            data = json.loads(clean.strip())
            return {
                "answer":              data.get("answer", raw),
                "decision_needed":     bool(data.get("decision_needed", False)),
                "decision_card":       data.get("decision_card"),
                "suggested_followups": data.get("suggested_followups", [])[:3],
                "urgency":             data.get("urgency", "medium"),
                "topic":               data.get("topic", "general"),
            }
        except (json.JSONDecodeError, AttributeError):
            return {
                "answer":              raw,
                "decision_needed":     False,
                "decision_card":       None,
                "suggested_followups": [],
                "urgency":             "medium",
                "topic":               "general",
            }

    @staticmethod
    def _build_fallback(question: str, context: str) -> dict:
        q = question.lower()
        if any(k in q for k in ["aging", "créance", "overdue", "receivable"]):
            answer = "Check the receivables panel. Focus on accounts with >60% overdue ratio."
            followups = ["Which accounts are most overdue?", "What is the total overdue amount?", "Who are the top 5 debtors?"]
        elif any(k in q for k in ["inventory", "stock", "جرد"]):
            answer = "Check the inventory panel for current stock levels by branch and product."
            followups = ["Which products are out of stock?", "What is the total inventory value?", "Which branch has the most stock?"]
        elif any(k in q for k in ["customer", "client", "عميل"]):
            answer = "Review the customers panel for active accounts and contact details."
            followups = ["How many active customers do we have?", "Which customers are at churn risk?", "Who are the top revenue customers?"]
        elif any(k in q for k in ["branch", "فرع", "succursale"]):
            answer = "Check the branches panel for all active locations and their performance."
            followups = ["How many branches are active?", "Which branch has the highest sales?", "Which branch needs restocking?"]
        elif any(k in q for k in ["movement", "mouvement", "حركة", "purchase", "achat", "transfer"]):
            answer = "All movement types (sales, purchases, transfers) are available in the transactions panel."
            followups = ["What are the total purchases this month?", "Show me all movement types.", "What is the stock balance after movements?"]
        elif any(k in q for k in ["risk", "critical", "urgent"]):
            answer = "Check the critical situations panel for urgent items requiring immediate action."
            followups = ["Which stock items need emergency reorder?", "Which customers should I call first?", "What is my total financial exposure?"]
        else:
            answer = "AI analysis temporarily unavailable. Please check your dashboard panels for the latest KPIs."
            followups = ["What are my top business risks?", "Which customers need urgent attention?", "What is my revenue outlook?"]

        return {
            "answer":              answer,
            "decision_needed":     False,
            "decision_card":       None,
            "suggested_followups": followups,
            "urgency":             "medium",
            "topic":               "general",
        }