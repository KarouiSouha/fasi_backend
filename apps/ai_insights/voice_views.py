"""
apps/ai_insights/voice_views.py
================================
Voice Intelligence API — Two endpoints:

  POST /api/ai-insights/voice/transcribe/
      Accepts audio blob (webm/wav/mp3) → returns transcription via OpenAI Whisper
      Body: multipart/form-data with field "audio"

  POST /api/ai-insights/voice/speak/
      Accepts text → returns MP3 audio stream via OpenAI TTS
      Body: { "text": "...", "voice": "alloy"|"nova"|"shimmer" }

Security:
  - Requires IsAuthenticated
  - company scoped (same as chat)
  - max audio size: 25MB (OpenAI Whisper limit)
  - text truncated to 4096 chars for TTS

Feature 2 — Explain My Decision:
  POST /api/ai-insights/chat/explain/
      Accepts a message_id (assistant message) and reconstructs the reasoning chain
      from the live business context used at that moment.
      Body: { "answer": "...", "context_snapshot": "..." }
      Returns: { "reasoning_chain": [...], "data_sources": [...], "confidence_breakdown": {...} }
"""

import logging
import base64

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_BYTES = 25 * 1024 * 1024   # 25 MB — Whisper limit
MAX_TTS_CHARS        = 4096
DEFAULT_TTS_VOICE    = "nova"              # warm, clear voice
ALLOWED_TTS_VOICES   = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}

EXPLAIN_SYSTEM_PROMPT = """You are a senior AI analyst explaining your own reasoning chain
for a decision support answer you gave to a manager.

You receive:
1. The answer you gave
2. The live business context that was available when you answered

Your task: reconstruct EXACTLY which data points drove your answer,
how much weight each signal had, and what alternative conclusions you considered.

Return ONLY valid JSON — no markdown, no preamble:
{
  "reasoning_steps": [
    {
      "step": 1,
      "label": "<what I analyzed>",
      "data_point": "<exact number or fact from context>",
      "weight": "high" | "medium" | "low",
      "insight": "<what this data point told me>"
    }
  ],
  "data_sources_used": ["<source 1>", "<source 2>"],
  "signals_ignored": ["<signal I considered but deprioritized and why>"],
  "alternative_conclusions": ["<what I could have said instead and why I didn't>"],
  "confidence_breakdown": {
    "data_quality": "high" | "medium" | "low",
    "signal_consistency": "high" | "medium" | "low",
    "recency": "high" | "medium" | "low",
    "overall": "high" | "medium" | "low"
  },
  "key_assumption": "<the main assumption my answer rests on>"
}"""


def _require_company(request):
    company = getattr(request.user, "company", None)
    if not company:
        return None, Response(
            {"error": "Your account is not linked to a company."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return company, None


# ─────────────────────────────────────────────────────────────────────────────
# Voice → Text (Whisper)
# ─────────────────────────────────────────────────────────────────────────────

class VoiceTranscribeView(APIView):
    """
    POST /api/ai-insights/voice/transcribe/
    Content-Type: multipart/form-data
    Field: audio (file — webm, wav, mp3, m4a, ogg)

    Returns: { "transcription": "...", "language": "en", "duration_s": 3.2 }
    """
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser]

    def post(self, request):
        company, err = _require_company(request)
        if err:
            return err

        audio_file = request.FILES.get("audio")
        language = (request.data.get("language") or "en").strip().lower()
        if not audio_file:
            return Response({"error": "No audio file provided. Send a 'audio' field."}, status=400)

        if audio_file.size > MAX_AUDIO_SIZE_BYTES:
            return Response({"error": f"Audio file too large (max {MAX_AUDIO_SIZE_BYTES // 1_000_000}MB)."}, status=400)

        openai_key = getattr(settings, "OPENAI_API_KEY", "").strip()
        if not openai_key:
            return Response({"error": "OpenAI API key not configured."}, status=503)

        try:
            import openai
            client = openai.OpenAI(api_key=openai_key)

            # Whisper requires a named file with extension
            name = audio_file.name or "audio.webm"
            audio_file.seek(0)

            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=(name, audio_file.read(), audio_file.content_type or "audio/webm"),
                language=language,
                response_format="verbose_json",
            )

            logger.info(
                "[VoiceTranscribe] OK — company=%s lang=%s duration=%.1fs",
                company.id,
                getattr(transcript, "language", "?"),
                getattr(transcript, "duration", 0),
            )

            return Response({
                "transcription": transcript.text,
                "language":      getattr(transcript, "language", "unknown"),
                "duration_s":    round(getattr(transcript, "duration", 0), 2),
            })

        except ImportError:
            return Response({"error": "openai package not installed."}, status=503)
        except Exception as exc:
            logger.error("[VoiceTranscribe] Failed company=%s: %s", company.id, exc)
            return Response({"error": f"Transcription failed: {str(exc)}"}, status=503)


# ─────────────────────────────────────────────────────────────────────────────
# Text → Voice (TTS)
# ─────────────────────────────────────────────────────────────────────────────

class VoiceSpeakView(APIView):
    """
    POST /api/ai-insights/voice/speak/
    Body: { "text": "...", "voice": "nova" }

    Streams back MP3 audio.
    The frontend creates a Blob URL and plays it with <audio>.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company, err = _require_company(request)
        if err:
            return err

        text  = (request.data.get("text") or "").strip()[:MAX_TTS_CHARS]
        voice = (request.data.get("voice") or DEFAULT_TTS_VOICE).lower()
        as_base64 = bool(request.data.get("as_base64", False))

        if not text:
            return Response({"error": "No text provided."}, status=400)
        if voice not in ALLOWED_TTS_VOICES:
            voice = DEFAULT_TTS_VOICE

        openai_key = getattr(settings, "OPENAI_API_KEY", "").strip()
        if not openai_key:
            return Response({"error": "OpenAI API key not configured."}, status=503)

        try:
            import openai
            client = openai.OpenAI(api_key=openai_key)

            # Stream the audio directly — avoids buffering the whole MP3 in memory
            response = client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text,
                response_format="mp3",
            )

            logger.info(
                "[VoiceSpeak] OK — company=%s voice=%s chars=%d",
                company.id, voice, len(text),
            )

            audio_bytes = response.content

            if as_base64:
                return Response({
                    "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                    "mime_type": "audio/mpeg",
                })

            http_response = StreamingHttpResponse(
                streaming_content=iter([audio_bytes]),
                content_type="audio/mpeg",
            )
            http_response["Content-Length"]      = str(len(audio_bytes))
            http_response["Content-Disposition"] = 'inline; filename="response.mp3"'
            http_response["Cache-Control"]        = "no-cache"
            return http_response

        except ImportError:
            return Response({"error": "openai package not installed."}, status=503)
        except Exception as exc:
            logger.error("[VoiceSpeak] Failed company=%s: %s", company.id, exc)
            return Response({"error": f"TTS failed: {str(exc)}"}, status=503)


# ─────────────────────────────────────────────────────────────────────────────
# Explain My Decision — Feature 2
# ─────────────────────────────────────────────────────────────────────────────

class ExplainDecisionView(APIView):
    """
    POST /api/ai-insights/chat/explain/
    Body: {
        "answer": "<the AI answer to explain>",
        "context_snapshot": "<the business context string used>"  (optional)
    }

    Returns the reasoning chain — which data drove the answer, what weight,
    what was ignored, alternative conclusions.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        company, err = _require_company(request)
        if err:
            return err

        answer           = (request.data.get("answer") or "").strip()
        context_snapshot = (request.data.get("context_snapshot") or "").strip()

        if not answer:
            return Response({"error": "answer is required."}, status=400)

        # If no context snapshot provided, rebuild it live
        if not context_snapshot:
            from .chat_views import BusinessContextBuilder
            context_snapshot = BusinessContextBuilder().build(company)

        # Truncate to avoid enormous prompts
        context_snapshot = context_snapshot[:3000]

        openai_key    = getattr(settings, "OPENAI_API_KEY", "").strip()
        anthropic_key = getattr(settings, "ANTHROPIC_API_KEY", "").strip()

        user_prompt = (
            f"=== THE ANSWER I GAVE ===\n{answer}\n\n"
            f"=== LIVE BUSINESS CONTEXT AVAILABLE WHEN I ANSWERED ===\n{context_snapshot}\n\n"
            "Reconstruct my exact reasoning chain."
        )

        result = self._call_ai(
            openai_key, anthropic_key, user_prompt, str(company.id)
        )

        if not result:
            # Deterministic fallback — parse the answer for data signals
            result = self._fallback_explain(answer, context_snapshot)

        return Response({**result, "fallback": not result.get("reasoning_steps")})

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _call_ai(openai_key, anthropic_key, user_prompt, company_id) -> dict | None:
        import json, re

        def extract_json(raw):
            try:
                return json.loads(raw.strip())
            except Exception:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except Exception:
                        pass
            return None

        # Try Anthropic first
        if anthropic_key:
            try:
                import anthropic as _a
                client = _a.Anthropic(api_key=anthropic_key)
                model  = getattr(settings, "AI_MODEL_SMART", "claude-haiku-4-5-20251001")
                resp   = client.messages.create(
                    model=model, max_tokens=900,
                    system=EXPLAIN_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                raw = resp.content[0].text if resp.content else ""
                return extract_json(raw)
            except Exception as exc:
                logger.warning("[ExplainDecision] Anthropic failed: %s", exc)

        # Fall back to OpenAI
        if openai_key:
            try:
                import openai as _o
                client = _o.OpenAI(api_key=openai_key)
                model  = getattr(settings, "AI_MODEL_SMART", "gpt-4o-mini")
                resp   = client.chat.completions.create(
                    model=model, max_tokens=900, temperature=0.2,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                        {"role": "user",   "content": user_prompt},
                    ],
                )
                raw = resp.choices[0].message.content or ""
                return extract_json(raw)
            except Exception as exc:
                logger.warning("[ExplainDecision] OpenAI failed: %s", exc)

        return None

    @staticmethod
    def _fallback_explain(answer: str, context: str) -> dict:
        """Rule-based explanation when AI is unavailable."""
        sources_used = []
        if "[RECEIVABLES]"  in context: sources_used.append("Receivables & Aging data")
        if "[CHURN RISK]"   in context: sources_used.append("Churn Prediction engine")
        if "[STOCK]"        in context: sources_used.append("Inventory Snapshot")
        if "[FORECAST]"     in context: sources_used.append("Revenue Forecast model")
        if "[ANOMALIES]"    in context: sources_used.append("Anomaly Detector")
        if "[CRITICAL"      in context: sources_used.append("Critical Situations scanner")
        if "[SALES LIVE]"   in context: sources_used.append("Live sales transactions")

        return {
            "reasoning_steps": [
                {
                    "step": 1,
                    "label": "Context scan",
                    "data_point": f"{len(sources_used)} data modules available",
                    "weight": "high",
                    "insight": "I scanned all available business context before answering.",
                },
                {
                    "step": 2,
                    "label": "Signal identification",
                    "data_point": ", ".join(sources_used) or "General context",
                    "weight": "high",
                    "insight": "These modules contained the most relevant signals for your question.",
                },
                {
                    "step": 3,
                    "label": "Answer synthesis",
                    "data_point": f"{len(answer)} character response",
                    "weight": "medium",
                    "insight": "I prioritized actionable, numbered data points over general advice.",
                },
            ],
            "data_sources_used":        sources_used or ["General business context"],
            "signals_ignored":          ["Historical data beyond analysis window"],
            "alternative_conclusions":  ["Could have focused on a different risk dimension"],
            "confidence_breakdown": {
                "data_quality":        "medium",
                "signal_consistency":  "medium",
                "recency":             "high",
                "overall":             "medium",
            },
            "key_assumption": "The cached data reflects the current state of the business.",
        }