"""
apps/ai_insights/chat_views.py
================================
AI Decision Advisor — POST /api/ai-insights/chat/

v3.0 — Support multi-société :
  - _get_authorized_companies() récupère toutes les sociétés de l'utilisateur
  - companies passé à RagService → DirectAnswerService
  - Questions groupe (total groupe, comparaison sociétés) supportées
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
    Construit un contexte textuel riche depuis tous les caches des analyzers.
    Inclut les noms de clients pour les managers (nécessaires pour les décisions).
    Supporte le multi-société.
    """

    def build(self, company, user_role: str = "manager", companies: list = None) -> str:
        lines = []
        # Info multi-société
        if companies and len(companies) > 1:
            lines.append(
                f"[COMPANIES] User has access to {len(companies)} companies: "
                + ", ".join(c.name for c in companies[:10])
            )

        self._add_credit_context(company, lines)
        self._add_critical_context(company, lines, user_role)
        self._add_churn_context(company, lines)
        self._add_stock_context(company, lines)
        self._add_forecast_context(company, lines)
        self._add_seasonal_context(company, lines)
        self._add_anomaly_context(company, lines)
        self._add_sales_context(company, lines)

        if not lines:
            lines.append(
                "No cached data — ask the manager to refresh the dashboards first."
            )
        return "\n".join(lines)

    def _add_credit_context(self, company, lines):
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

            top_overdue = list(
                qs.filter(total__gt=0).order_by("-total")
                .values("account", "account_code", "total", "current")[:5]
            )
            lines.append(
                f"[RECEIVABLES] Total: {grand:,.0f} LYD | "
                f"Overdue: {overdue:,.0f} LYD ({or_pct}%) | "
                f"Snapshot: {snap.uploaded_at.date()}"
            )
            for r in top_overdue:
                rec_overdue = max(0, float(r["total"]) - float(r["current"]))
                if rec_overdue > 0:
                    lines.append(
                        f"  · {r['account'][:60]}: "
                        f"{float(r['total']):,.0f} LYD total, "
                        f"{rec_overdue:,.0f} LYD overdue"
                    )
        except Exception as exc:
            logger.debug("[Chat] credit context failed: %s", exc)

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
            f"[STOCK] Class A: {s.get('class_a_count',0)} SKUs | "
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
        data = cache.get(_cache_key("anomaly", str(company.id), ai=1))
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
    MAX_CONTEXT_LEN = 3500

    def post(self, request):
        company, err = _require_company(request)
        if err:
            return err

        # ── Récupérer toutes les sociétés autorisées ──────────────────────────
        companies = self._get_authorized_companies(request.user)

        messages        = request.data.get("messages", [])
        conversation_id = request.data.get("conversation_id")
        language        = request.data.get("language", "English")

        conversation = None
        if conversation_id:
            conversation = (
                AIConversation.objects
                .filter(id=conversation_id, company=company, user=request.user)
                .first()
            )
            if not conversation:
                return Response({"error": "Conversation not found."}, status=404)

        if not messages:
            return Response({"error": "messages is required."}, status=400)

        messages = messages[-self.MAX_HISTORY:]

        # Contexte business avec info multi-société
        context = BusinessContextBuilder().build(
            company,
            user_role=getattr(request.user, "role", "manager") or "manager",
            companies=companies,
        )
        if len(context) > self.MAX_CONTEXT_LEN:
            context = context[:self.MAX_CONTEXT_LEN] + "\n[context truncated]"

        system_prompt = SYSTEM_PROMPT.format(
            today=date.today().isoformat(),
            context=context,
            language=language,
        )

        api_messages = []
        for m in messages:
            role    = m.get("role", "user")
            content = m.get("content", "")
            if role not in ("user", "assistant") or not content:
                continue
            api_messages.append({"role": role, "content": content})

        if not api_messages:
            return Response({"error": "No valid messages."}, status=400)

        latest_user_message = next(
            (m["content"] for m in reversed(api_messages) if m["role"] == "user"),
            "",
        )
        if not latest_user_message:
            return Response({"error": "A user message is required."}, status=400)

        if conversation:
            persisted_history = list(
                conversation.messages.order_by("created_at")
                .values("role", "content")
            )
            persisted_history = persisted_history[-(self.MAX_HISTORY - 1):]
            api_messages = [
                {"role": m["role"], "content": m["content"]}
                for m in persisted_history
                if m["role"] in ("user", "assistant") and m["content"]
            ]
            api_messages.append({"role": "user", "content": latest_user_message})

        if not conversation:
            conversation = AIConversation.objects.create(
                company=company,
                user=request.user,
                title=_conversation_title_from_text(latest_user_message),
            )

        user_message_obj = AIConversationMessage.objects.create(
            conversation=conversation,
            role=AIConversationMessage.Role.USER,
            content=latest_user_message,
            metadata={},
        )

        # ── RAG pipeline avec support multi-société ───────────────────────────
        try:
            from django.conf import settings
            if getattr(settings, "AI_RAG_ENABLED", False):
                try:
                    from .services.rag_service import RagService
                    rag_result = RagService().run(
                        latest_user_message,
                        company,
                        context,
                        companies=companies,   # ← multi-société
                    )
                    if rag_result and rag_result.get("answer"):
                        reply = rag_result
                    else:
                        reply = self._call_ai(system_prompt, api_messages, company)
                except Exception as exc:
                    logger.debug("[AIChatView] RAG failed: %s", exc)
                    reply = self._call_ai(system_prompt, api_messages, company)
            else:
                reply = self._call_ai(system_prompt, api_messages, company)

            if reply:
                with transaction.atomic():
                    AIConversationMessage.objects.create(
                        conversation=conversation,
                        role=AIConversationMessage.Role.ASSISTANT,
                        content=reply.get("answer", ""),
                        metadata={
                            "decision_needed":   bool(reply.get("decision_needed", False)),
                            "decision_card":     reply.get("decision_card"),
                            "suggested_followups": reply.get("suggested_followups", []),
                            "urgency":           reply.get("urgency", "medium"),
                            "topic":             reply.get("topic", "general"),
                            "fallback":          False,
                            "reply_to":          str(user_message_obj.id),
                        },
                    )
                    conversation.save()
                return Response({
                    "conversation_id": str(conversation.id),
                    **reply,
                    "fallback": False,
                })
        except Exception as exc:
            logger.error(
                "[AIChatView] AI call failed company=%s: %s", company.id, exc
            )

        # ── Fallback ──────────────────────────────────────────────────────────
        fallback = self._build_fallback(latest_user_message, context)
        with transaction.atomic():
            AIConversationMessage.objects.create(
                conversation=conversation,
                role=AIConversationMessage.Role.ASSISTANT,
                content=fallback.get("answer", ""),
                metadata={
                    "decision_needed":   bool(fallback.get("decision_needed", False)),
                    "decision_card":     fallback.get("decision_card"),
                    "suggested_followups": fallback.get("suggested_followups", []),
                    "urgency":           fallback.get("urgency", "medium"),
                    "topic":             fallback.get("topic", "general"),
                    "fallback":          True,
                    "reply_to":          str(user_message_obj.id),
                },
            )
            conversation.save()
        return Response({
            "conversation_id": str(conversation.id),
            **fallback,
            "fallback": True,
        })

    # ── Sociétés autorisées ───────────────────────────────────────────────────

    @staticmethod
    def _get_authorized_companies(user) -> list:
        """
        Retourne toutes les sociétés auxquelles l'utilisateur a accès.

        Logique :
          1. Superuser / staff → toutes les sociétés
          2. Relation user.companies (ManyToMany) → ses sociétés
          3. Fallback → sa seule société (user.company)
        """
        from apps.companies.models import Company

        try:
            if user.is_superuser or getattr(user, "is_staff", False):
                return list(Company.objects.all())

            # ManyToMany "companies" si défini sur le modèle User
            if hasattr(user, "companies"):
                cos = list(user.companies.all())
                if cos:
                    return cos
        except Exception as exc:
            logger.debug("[AIChatView] _get_authorized_companies: %s", exc)

        # Fallback : société unique
        if getattr(user, "company", None):
            return [user.company]

        return []

    # ── Appel LLM ─────────────────────────────────────────────────────────────

    def _call_ai(self, system_prompt: str, messages: list, company) -> dict | None:
        from django.conf import settings

        openai_key    = getattr(settings, "OPENAI_API_KEY", "").strip()

        # OpenAI
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
                logger.info(
                    "[AIChatView] OpenAI OK company=%s model=%s tokens=%d",
                    company.id, model,
                    resp.usage.total_tokens if resp.usage else 0,
                )
                return self._parse_response(raw)
            except ImportError:
                logger.error("[AIChatView] openai package not installed")
            except Exception as exc:
                logger.error("[AIChatView] OpenAI failed company=%s: %s", company.id, exc)

        logger.error(
            "[AIChatView] No AI provider available company=%s. "
            "Set OPENAI_API_KEY.",
            company.id,
        )
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
        if any(k in q for k in ["risk", "critical", "urgent"]):
            answer = (
                "Check the critical situations panel for urgent items. "
                "Focus on 'immediate' urgency — action needed today."
            )
            followups = [
                "Which stock items need emergency reorder?",
                "Which customers should I call first?",
                "What is my total financial exposure?",
            ]
        elif any(k in q for k in ["churn", "customer", "inactive"]):
            answer = (
                "Review your churn risk panel for 'critical' or 'high' customers. "
                "These have the longest inactivity and highest revenue at risk."
            )
            followups = [
                "What is the revenue at risk from churning customers?",
                "How do I prioritize outreach calls?",
                "Which customers are most profitable?",
            ]
        elif any(k in q for k in ["stock", "reorder", "inventory"]):
            answer = (
                "Check your stock optimizer for 'immediate' urgency items. "
                "Class A items approaching stockout are highest priority."
            )
            followups = [
                "What is the EOQ for my top SKUs?",
                "Which products are completely out of stock?",
                "How much revenue am I losing to stockouts?",
            ]
        elif any(k in q for k in ["forecast", "predict", "revenue"]):
            answer = (
                "Your 3-month forecast is in the Forecast panel. "
                "Check P10/P50/P90 range for confidence interval."
            )
            followups = [
                "What is my cash flow projection for next month?",
                "What is the main risk to my forecast?",
                "How does this compare to last year?",
            ]
        else:
            answer = (
                "AI analysis temporarily unavailable. "
                "Please check your dashboard panels for the latest KPIs."
            )
            followups = [
                "What are my top business risks?",
                "Which customers need urgent attention?",
                "What is my revenue outlook?",
            ]

        return {
            "answer":              answer,
            "decision_needed":     False,
            "decision_card":       None,
            "suggested_followups": followups,
            "urgency":             "medium",
            "topic":               "general",
        }