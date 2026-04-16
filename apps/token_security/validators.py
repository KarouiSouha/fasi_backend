import hashlib
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import AuthenticationFailed
from .models import TokenBlacklist


class TokenVersionValidator:
    """
    Checks that the token version matches the user's current version.

    When a password change or reset occurs, the user's token_version field
    is incremented in the database. Any token containing an older version
    automatically becomes invalid, without needing to blacklist them one by one.
    """

    def validate(self, token_payload: dict, user) -> None:
        """
        Args:
            token_payload : decoded JWT payload
            user          : User instance retrieved from the database

        Raises:
            AuthenticationFailed if the token version is obsolete.
        """
        token_version = token_payload.get("token_version")

        if token_version is None:
            raise AuthenticationFailed(
                _("Invalid token: version missing."),
                code="token_version_missing",
            )

        if int(token_version) != user.token_version:
            raise AuthenticationFailed(
                _("Session expired. Your password has been changed. Please log in again."),
                code="token_version_mismatch",
            )


class BlacklistValidator:
    """
    Checks that the JTI (JWT ID) of the token is not present in the blacklist.
    Consulted on every authenticated request via CustomJWTAuthentication.
    """

    def validate(self, token_jti: str) -> None:
        """
        Args:
            token_jti : unique identifier of the token ("jti" field from the payload)

        Raises:
            AuthenticationFailed if the token has been revoked.
        """
        if TokenBlacklist.objects.filter(token_jti=token_jti).exists():
            raise AuthenticationFailed(
                _("Token has been revoked. Please log in again."),
                code="token_blacklisted",
            )


class DeviceValidator:
    """
    Verifies that the device fingerprint in the token matches the one
    recorded during login.

    Protects against token theft: even if an attacker steals a token,
    they cannot use it from a different device.
    """

    def validate(self, token_payload: dict, current_fingerprint: str) -> None:
        """
        Args:
            token_payload        : decoded JWT payload
            current_fingerprint  : fingerprint of the current request's device

        Raises:
            AuthenticationFailed if the fingerprint does not match.
        """
        stored_fp = token_payload.get("device_fp")

        if not stored_fp:
            # Token generated without fingerprint (legacy format): we tolerate it
            return

        current_fp_hash = hashlib.sha256(current_fingerprint.encode()).hexdigest()

        if stored_fp != current_fp_hash:
            raise AuthenticationFailed(
                _("Unrecognized device. Invalid session."),
                code="device_fingerprint_mismatch",
            )


class IPValidator:
    """
    Checks whether the request's IP address matches the one recorded during login.

    Unlike DeviceValidator, this validator does NOT automatically block the request
    (mobile users often change IP addresses).
    It returns a warning signal used by SuspiciousActivityMiddleware.
    """

    def validate(self, token_payload: dict, current_ip: str) -> bool:
        """
        Args:
            token_payload : decoded JWT payload
            current_ip    : IP address of the current request

        Returns:
            True  : IP matches (normal situation)
            False : IP has changed (possible suspicious activity, should be logged)
        """
        stored_ip_hash = token_payload.get("ip_hash")

        if not stored_ip_hash:
            return True

        current_ip_hash = hashlib.sha256(current_ip.encode()).hexdigest()
        return stored_ip_hash == current_ip_hash