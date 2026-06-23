import logging
from django.contrib.auth import get_user_model
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken

from .models import ActiveSession, LoginAttempt
from .serializers import (
    ActiveSessionSerializer,
    TokenRefreshInputSerializer,
    RevokeSessionInputSerializer,
)
from .services import TokenService
from .tokens import CustomRefreshToken
from .utils import get_client_ip
from .validators import BlacklistValidator, TokenVersionValidator, DeviceValidator

security_logger = logging.getLogger("security")


class LoginView(APIView):
    """
    POST /api/auth/login/

    Authenticates a user and returns a JWT token pair.

    Request body:
        - email    : user's email address
        - password : password

    Success response (200):
        - access     : access JWT token (expires in 60 minutes)
        - refresh    : refresh JWT token (expires in 7 days)
        - session_id : ID of the created session
        - user       : basic information of the logged-in user

    Possible errors:
        - 400 : missing data
        - 401 : invalid credentials
        - 403 : account pending / rejected / suspended
        - 429 : too many attempts (handled by RateLimitLoginMiddleware)
    """
    permission_classes = [AllowAny]

    def post(self, request):
        from django.contrib.auth import get_user_model, authenticate
        User = get_user_model()

        email = request.data.get("email", "").strip().lower()
        password = request.data.get("password", "")

        if not email or not password:
            return Response(
                {"error": "Email and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Authenticate credentials
        user = authenticate(request, username=email, password=password)

        if user is None:
            # Log failed attempt
            LoginAttempt.objects.create(
                email=email,
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                is_successful=False,
                failure_reason="invalid_credentials",
            )
            return Response(
                {"error": "Invalid email or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Check account status
        account_status_error = self._check_account_status(user)
        if account_status_error:
            LoginAttempt.objects.create(
                email=email,
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                is_successful=False,
                failure_reason=account_status_error["code"],
            )
            return Response(
                {"error": account_status_error["message"]},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Log successful attempt
        LoginAttempt.objects.create(
            email=email,
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            is_successful=True,
        )

        # Generate tokens and create session
        tokens = TokenService.issue_tokens(user=user, request=request)

        return Response(
            {
                **tokens,
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "full_name": user.get_full_name(),
                    "role": user.role,
                    "must_change_password": user.must_change_password,
                },
            },
            status=status.HTTP_200_OK,
        )

    def _check_account_status(self, user) -> dict | None:
        """
        Checks if the account is allowed to log in.

        Returns:
            None if the account is valid.
            Dict with "code" and "message" if the account is blocked.
        """
        status_messages = {
            "pending": {
                "code": "account_pending",
                "message": "Your account is pending approval by an administrator.",
            },
            "rejected": {
                "code": "account_rejected",
                "message": "Your access request has been rejected. Please contact an administrator.",
            },
            "suspended": {
                "code": "account_suspended",
                "message": "Your account has been suspended. Please contact an administrator.",
            },
        }
        # Basic status (pending/rejected/suspended)
        base_status = status_messages.get(user.status)
        if base_status:
            return base_status

        # Additional checks for managers:
        # - `is_verified` must be True (admin approval)
        if getattr(user, "is_manager", False):
            if not getattr(user, "is_verified", False):
                return {
                    "code": "admin_not_approved",
                    "message": "Your account is awaiting administrator approval.",
                }

        return None

 
class RefreshView(APIView):
    """
    POST /api/auth/token/refresh/
 
    ── CHANGES vs original ──────────────────────────────────────────────────
    Two guards added BEFORE rotation:
 
    1. Blacklist check
       logout-all blacklists every refresh-token JTI. Without this check,
       the web could still call this endpoint and receive fresh tokens,
       effectively ignoring the global logout.
 
    2. token_version check
       logout-all also increments user.token_version. Checking it here means
       any device whose token_version is stale is rejected immediately, even
       if its JTI was not yet in the blacklist (e.g. race condition).
 
    Both guards return 401, which the web client handles by clearing
    localStorage and redirecting to /login (see refreshAccessToken in api.ts).
    ─────────────────────────────────────────────────────────────────────────
    """
    permission_classes = [AllowAny]
 
    def post(self, request):
        serializer = TokenRefreshInputSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
 
        raw_refresh_token = serializer.validated_data["refresh"]
 
        try:
            # Decode & verify JWT signature / expiry
            refresh_token = CustomRefreshToken(raw_refresh_token)
            token_payload = refresh_token.payload
            token_jti     = refresh_token["jti"]
 
            # ── Guard 1: blacklist ────────────────────────────────────────
            # logout-all / revoke-session blacklists the JTI server-side.
            # Without this check the web simply gets new tokens and stays
            # logged in despite the global logout.
            if TokenBlacklist.objects.filter(token_jti=token_jti).exists():
                security_logger.info(
                    f"[RefreshView] Rejected blacklisted JTI {token_jti[:12]}…"
                )
                return Response(
                    {
                        "error": "Your session has been revoked. Please log in again.",
                        "code":  "token_blacklisted",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )
 
            # ── Guard 2: token_version ────────────────────────────────────
            # logout-all increments user.token_version. Any token issued
            # before that bump is now stale and must be rejected.
            from django.contrib.auth import get_user_model
            User     = get_user_model()
            user_id  = token_payload.get("user_id")
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                return Response(
                    {"error": "User not found.", "code": "user_not_found"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
 
            token_version = token_payload.get("token_version")
            if token_version is not None and int(token_version) != user.token_version:
                security_logger.info(
                    f"[RefreshView] Stale token_version for {user.email}: "
                    f"token={token_version} db={user.token_version}"
                )
                return Response(
                    {
                        "error": "Session expired due to a security event. Please log in again.",
                        "code":  "token_version_mismatch",
                    },
                    status=status.HTTP_401_UNAUTHORIZED,
                )
 
            # ── Rotate ───────────────────────────────────────────────────
            new_tokens = TokenService.rotate_refresh_token(
                old_refresh_token_payload=token_payload,
                request=request,
            )
            return Response(new_tokens, status=status.HTTP_200_OK)
 
        except ValueError as e:
            # Token reuse detected — TokenService already revoked everything
            return Response(
                {"error": str(e), "code": "token_reuse_detected"},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except TokenError as e:
            raise InvalidToken(e.args[0])
 
 
class LogoutView(APIView):
    """POST /api/auth/logout/ — unchanged."""
    permission_classes = [IsAuthenticated]
 
    def post(self, request):
        raw_refresh_token = request.data.get("refresh")
        if not raw_refresh_token:
            return Response(
                {"error": "Refresh token is required to log out."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            refresh_token = CustomRefreshToken(raw_refresh_token)
            TokenService.revoke_token(
                token_jti=refresh_token["jti"],
                user=request.user,
                token_type="refresh",
                reason="logout",
            )
            return Response({"message": "Successfully logged out."}, status=status.HTTP_200_OK)
        except TokenError:
            return Response({"error": "Invalid refresh token."}, status=status.HTTP_400_BAD_REQUEST)
 
class LogoutView(APIView):
    """
    POST /api/auth/logout/

    Logs the user out from the current device only.
    Revokes the refresh token of the current session.

    Request body:
        - refresh : refresh token of the session to close

    Success response (200):
        - message : logout confirmation
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        raw_refresh_token = request.data.get("refresh")

        if not raw_refresh_token:
            return Response(
                {"error": "Refresh token is required to log out."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            refresh_token = CustomRefreshToken(raw_refresh_token)
            token_jti = refresh_token["jti"]

            TokenService.revoke_token(
                token_jti=token_jti,
                user=request.user,
                token_type="refresh",
                reason="logout",
            )

            return Response(
                {"message": "Successfully logged out."},
                status=status.HTTP_200_OK,
            )

        except TokenError:
            return Response(
                {"error": "Invalid refresh token."},
                status=status.HTTP_400_BAD_REQUEST,
            )


class LogoutAllView(APIView):
    """
    POST /api/auth/logout-all/

    Logs the user out from ALL their devices simultaneously.
    Revokes all active sessions.

    No request body required.

    Success response (200):
        - message          : confirmation
        - sessions_revoked : number of sessions revoked
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        count = TokenService.revoke_all_user_tokens(
            user=request.user,
            reason="logout_all",
        )

        return Response(
            {
                "message": "Successfully logged out from all your devices.",
                "sessions_revoked": count,
            },
            status=status.HTTP_200_OK,
        )


class ActiveSessionsView(APIView):
    """
    GET /api/auth/sessions/

    Returns the list of all devices currently connected to the authenticated user's account.

    Success response (200):
        - sessions : list of active sessions with device_name, ip, last_activity
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sessions = ActiveSession.objects.filter(user=request.user).order_by("-last_activity")
        serializer = ActiveSessionSerializer(sessions, many=True)

        return Response(
            {"sessions": serializer.data},
            status=status.HTTP_200_OK,
        )


class RevokeSessionView(APIView):
    """
    DELETE /api/auth/sessions/{session_id}/

    Revokes a specific session remotely (logs out a particular device).
    The user can only revoke their own sessions.

    URL parameter:
        - session_id : UUID of the session to revoke

    Success response (200):
        - message : confirmation
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, session_id):
        try:
            session = ActiveSession.objects.get(
                id=session_id,
                user=request.user,
            )
        except ActiveSession.DoesNotExist:
            return Response(
                {"error": "Session not found or access denied."},
                status=status.HTTP_404_NOT_FOUND,
            )

        TokenService.revoke_token(
            token_jti=session.refresh_token_jti,
            user=request.user,
            token_type="refresh",
            reason="admin_revoked",
        )

        return Response(
            {"message": "Session revoked successfully."},
            status=status.HTTP_200_OK,
        )