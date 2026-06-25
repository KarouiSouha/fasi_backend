"""
apps/authentication/email_verification_views.py

Email verification step for new Manager signups.

Flow:
    POST /api/users/signup/
        → Account created (status=PENDING, is_email_verified=False)
        → Email sent to manager with a link to the FRONTEND verify page

    Frontend page /signup/verify-email?token=<token>
        → Calls GET /api/users/verify-email/?token=<token> via fetch/axios
        → Backend validates token, sets is_email_verified=True, returns JSON
        → Admin notified of the new manager request
        → Frontend shows "Account created! Awaiting verification by the admin."
"""

import logging
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
import secrets

logger = logging.getLogger("django")
User = get_user_model()

# Cache config
EMAIL_TOKEN_PREFIX = "email_verify"
EMAIL_TOKEN_EXPIRY = 24 * 60 * 60  # 24 hours


def generate_email_token() -> str:
    return secrets.token_urlsafe(32)


class VerifyManagerEmailView(APIView):
    """
    GET /api/users/verify-email/?token=<token>

    Called by the FRONTEND page (not by a direct email-link click).
    The frontend page reads the token from its own URL, then calls this
    endpoint with fetch/axios — exactly like login/signup already do —
    and handles the redirect itself once it gets a response.

    - Validates the token
    - Sets is_email_verified=True on the manager
    - Notifies all admins
    - Returns JSON (never an HttpResponseRedirect)
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        token = request.query_params.get("token", "").strip()

        if not token:
            return Response(
                {"error": "Missing verification token.", "reason": "missing_token"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"{EMAIL_TOKEN_PREFIX}:{token}"
        data = cache.get(cache_key)

        if not data:
            logger.warning(f"[EMAIL VERIFY] Invalid or expired token: {token[:12]}...")
            return Response(
                {"error": "This verification link is invalid or has expired.", "reason": "expired"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            manager = User.objects.get(id=data["user_id"], role=User.Role.MANAGER)
        except User.DoesNotExist:
            logger.error(f"[EMAIL VERIFY] User not found for token data: {data}")
            return Response(
                {"error": "Account not found.", "reason": "not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Already verified — idempotent success
        if manager.is_email_verified:
            logger.info(f"[EMAIL VERIFY] Already verified: {manager.email}")
            return Response(
                {
                    "message": "Account created! Awaiting verification by the admin.",
                    "already_verified": True,
                },
                status=status.HTTP_200_OK,
            )

        # Mark email as verified
        manager.is_email_verified = True
        manager.save(update_fields=["is_email_verified", "updated_at"])

        # Remove used token
        cache.delete(cache_key)

        # Notify admins now that the email is confirmed
        try:
            from apps.authentication.services import EmailService
            EmailService.send_admin_new_manager_request(manager)
            logger.info(f"[EMAIL VERIFY] Admin notified for manager {manager.email}")
        except Exception as e:
            logger.error(f"[EMAIL VERIFY] Failed to notify admin: {e}")

        logger.info(f"[EMAIL VERIFY] Email verified successfully for {manager.email}")
        return Response(
            {
                "message": "Account created! Awaiting verification by the admin.",
                "already_verified": False,
            },
            status=status.HTTP_200_OK,
        )


class ResendVerificationEmailView(APIView):
    """
    POST /api/users/resend-verification/

    Body: { "email": "manager@example.com" }

    Resends the verification email if the manager hasn't verified yet.
    Throttled: max 1 resend per 2 minutes.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = request.data.get("email", "").strip().lower()
        if not email:
            return Response(
                {"error": "Email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            manager = User.objects.get(email=email, role=User.Role.MANAGER)
        except User.DoesNotExist:
            # Silent — don't leak existence
            return Response(
                {"message": "If this email is registered and not yet verified, a new email has been sent."},
                status=status.HTTP_200_OK,
            )

        if manager.is_email_verified:
            return Response(
                {"message": "This email address is already verified."},
                status=status.HTTP_200_OK,
            )

        # Throttle: check if a token was sent recently
        throttle_key = f"{EMAIL_TOKEN_PREFIX}_throttle:{email}"
        if cache.get(throttle_key):
            return Response(
                {"error": "Please wait 2 minutes before requesting another verification email."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Issue new token and send
        _issue_and_send_verification(manager)

        # Throttle for 2 minutes
        cache.set(throttle_key, True, timeout=120)

        logger.info(f"[EMAIL VERIFY] Resent verification email to {email}")
        return Response(
            {"message": "If this email is registered and not yet verified, a new email has been sent."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Helper used by signup_views.py
# ---------------------------------------------------------------------------

def _issue_and_send_verification(manager) -> None:
    token = generate_email_token()
    cache_key = f"{EMAIL_TOKEN_PREFIX}:{token}"
    cache.set(cache_key, {"user_id": str(manager.id)}, timeout=EMAIL_TOKEN_EXPIRY)

    from apps.authentication.services import EmailService
    print(f"[DEBUG] Sending verification email to {manager.email}", flush=True)
    EmailService.send_email_verification(manager=manager, token=token)
    print(f"[DEBUG] Email sent successfully to {manager.email}", flush=True)