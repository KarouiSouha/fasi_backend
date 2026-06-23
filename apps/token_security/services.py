import logging
from datetime import datetime, timezone

from django.utils.translation import gettext_lazy as _

from .models import TokenBlacklist, ActiveSession, RefreshTokenRotation
from .tokens import CustomAccessToken, CustomRefreshToken, TemporaryToken
from .utils import get_client_ip, get_device_fingerprint, parse_device_name

security_logger = logging.getLogger("security")


class TokenService:
    """
    Centralized service layer for all JWT token operations.

    This class is the single place where tokens are created, rotated, or revoked.
    No view should manipulate tokens directly without going through this service.
    """

    @staticmethod
    def issue_tokens(user, request) -> dict:
        """
        Generates an access + refresh token pair at login time.
        Creates an active session associated with the device and IP.

        Args:
            user    : authenticated User instance
            request : Django HTTP request (to extract IP and device)

        Returns:
            dict containing:
                - access     : signed JWT access token
                - refresh    : signed JWT refresh token
                - session_id : UUID of the created session
        """
        ip_address = get_client_ip(request)
        device_fingerprint = get_device_fingerprint(request)
        device_name = parse_device_name(request.META.get("HTTP_USER_AGENT", ""))

        # Generate enriched tokens
        refresh_token = CustomRefreshToken.for_user_with_context(
            user=user,
            device_fingerprint=device_fingerprint,
            ip_address=ip_address,
        )
        access_token = refresh_token.access_token

        # Enrich the access token with role data
        access_token["role"] = user.role
        access_token["permissions"] = user.permissions_list
        access_token["token_version"] = user.token_version
        access_token["company_id"] = str(user.company_id) if user.company_id else None

        # Create the active session in the database
        session = ActiveSession.objects.create(
            user=user,
            refresh_token_jti=refresh_token["jti"],
            device_fingerprint=device_fingerprint,
            device_name=device_name,
            ip_address=ip_address,
        )

        security_logger.info(
            f"New session created for [{user.email}] "
            f"from {ip_address} on {device_name}."
        )

        return {
            "access": str(access_token),
            "refresh": str(refresh_token),
            "session_id": str(session.id),
        }

    @staticmethod
    def rotate_refresh_token(old_refresh_token_payload: dict, request) -> dict:
        """
        Performs refresh token rotation:
            1. Checks that the old refresh token has not already been used (token reuse attack)
            2. Blacklists the old token
            3. Generates a new refresh token
            4. Updates the active session

        Args:
            old_refresh_token_payload : decoded payload of the old refresh token
            request                   : current HTTP request

        Returns:
            dict containing the new access token and the new refresh token.

        Raises:
            ValueError if the old JTI has already been used (attack detected).
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()

        old_jti = old_refresh_token_payload["jti"]
        user_id = old_refresh_token_payload["user_id"]

        # Detect an attempt to reuse an old refresh token
        if RefreshTokenRotation.objects.filter(old_token_jti=old_jti).exists():
            # Immediately revoke ALL tokens for the user
            user = User.objects.get(id=user_id)
            TokenService.revoke_all_user_tokens(user, reason="token_reuse")

            security_logger.critical(
                f"ATTACK DETECTED: old refresh token reuse "
                f"for user [{user.email}]. "
                f"All tokens have been revoked."
            )
            raise ValueError("Refresh token already used. All your tokens have been revoked as a security measure.")

        user = User.objects.get(id=user_id)
        ip_address = get_client_ip(request)
        device_fingerprint = get_device_fingerprint(request)

        # Generate the new refresh token
        new_refresh_token = CustomRefreshToken.for_user_with_context(
            user=user,
            device_fingerprint=device_fingerprint,
            ip_address=ip_address,
        )
        new_access_token = new_refresh_token.access_token
        new_access_token["role"] = user.role
        new_access_token["permissions"] = user.permissions_list
        new_access_token["token_version"] = user.token_version
        new_access_token["company_id"] = str(user.company_id) if user.company_id else None

        # Record the rotation
        RefreshTokenRotation.objects.create(
            user=user,
            old_token_jti=old_jti,
            new_token_jti=new_refresh_token["jti"],
            ip_address=ip_address,
            device_fingerprint=device_fingerprint,
        )

        # Blacklist the old refresh token
        TokenService._blacklist_jti(
            jti=old_jti,
            user=user,
            token_type="refresh",
            reason="logout",
        )

        # Update the active session with the new JTI
        ActiveSession.objects.filter(
            user=user,
            refresh_token_jti=old_jti,
        ).update(refresh_token_jti=new_refresh_token["jti"])

        return {
            "access": str(new_access_token),
            "refresh": str(new_refresh_token),
        }

    @staticmethod
    def revoke_token(token_jti: str, user, token_type: str = "refresh", reason: str = "logout") -> None:
        """
        Revokes a specific token and deletes the associated session.

        Args:
            token_jti  : unique identifier of the token to revoke
            user       : token owner
            token_type : "access" | "refresh" | "temporary"
            reason     : revocation reason
        """
        TokenService._blacklist_jti(
            jti=token_jti,
            user=user,
            token_type=token_type,
            reason=reason,
        )

        # Delete the active session linked to this refresh token
        if token_type == "refresh":
            ActiveSession.objects.filter(
                user=user,
                refresh_token_jti=token_jti,
            ).delete()

        security_logger.info(
            f"Token revoked for [{user.email}] - Reason: {reason}."
        )

    @staticmethod
    def revoke_all_user_tokens(user, reason: str = "logout_all") -> int:
        """
        Revokes all active sessions for a user.
        Used during global logout or after suspicious activity is detected.

        Args:
            user   : user whose tokens are all revoked
            reason : revocation reason

        Returns:
            Number of revoked sessions.
        """
        # Invalidate all existing access tokens immediately by bumping the user's token version.
        # This forces every access token already issued to become invalid.
        user.token_version += 1
        user.save(update_fields=["token_version"])

        active_sessions = ActiveSession.objects.filter(user=user)
        count = active_sessions.count()

        for session in active_sessions:
            TokenService._blacklist_jti(
                jti=session.refresh_token_jti,
                user=user,
                token_type="refresh",
                reason=reason,
            )

        active_sessions.delete()

        security_logger.warning(
            f"All sessions revoked for [{user.email}] "
            f"({count} sessions). Reason: {reason}."
        )

        return count

    @staticmethod
    def get_active_sessions(user) -> list:
        """
        Returns the list of active sessions for the user.

        Returns:
            List of dicts containing information about each session.
        """
        sessions = ActiveSession.objects.filter(user=user).order_by("-last_activity")
        return [
            {
                "session_id": str(session.id),
                "device_name": session.device_name or "Unknown device",
                "ip_address": session.ip_address,
                "last_activity": session.last_activity,
                "created_at": session.created_at,
                "is_current": session.is_current,
            }
            for session in sessions
        ]

    @staticmethod
    def issue_temporary_token(user, action: str) -> str:
        """
        Generates a temporary token for password reset or email verification.

        Args:
            user   : user concerned
            action : "password_reset" | "email_verification"

        Returns:
            Temporary token as a signed string.
        """
        token = TemporaryToken.for_user_action(user=user, action=action)
        return str(token)

    @staticmethod
    def _blacklist_jti(jti: str, user, token_type: str, reason: str) -> None:
        """
        Internal method: adds a JTI to the blacklist.
        Avoids duplicates via get_or_create.
        """
        from django.utils import timezone as tz
        from datetime import timedelta

        # Calculate expiry date based on token type
        lifetimes = {
            "access": timedelta(minutes=60),
            "refresh": timedelta(days=7),
            "temporary": timedelta(hours=1),
        }
        expires_at = tz.now() + lifetimes.get(token_type, timedelta(days=1))

        TokenBlacklist.objects.get_or_create(
            token_jti=jti,
            defaults={
                "user": user,
                "token_type": token_type,
                "expires_at": expires_at,
                "reason": reason,
            },
        )