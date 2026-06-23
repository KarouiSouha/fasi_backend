from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from .validators import TokenVersionValidator, BlacklistValidator, DeviceValidator, IPValidator
from .utils import get_client_ip, get_device_fingerprint


class CustomJWTAuthentication(JWTAuthentication):
    """
    Custom JWT authentication backend.
    Replaces the default djangorestframework-simplejwt backend.

    Checks in order on every request:
        1. Signature and expiry validity (handled by simplejwt)
        2. Presence in the blacklist
        3. Token version (invalid if password was changed)
        4. Device fingerprint
        5. IP change (log only, does not block)

    Configure in settings/base.py:
        REST_FRAMEWORK = {
            "DEFAULT_AUTHENTICATION_CLASSES": [
                "apps.token_security.backends.CustomJWTAuthentication",
            ],
        }
    """

    blacklist_validator = BlacklistValidator()
    version_validator = TokenVersionValidator()
    device_validator = DeviceValidator()
    ip_validator = IPValidator()

    def authenticate(self, request):
        """
        Main entry point. Called by DRF on every request.

        Returns:
            Tuple (user, validated_token) if successfully authenticated.
            None if no token is present (anonymous request).

        Raises:
            AuthenticationFailed if the token is invalid, revoked, or suspicious.
        """
        # Retrieve token from the Authorization header
        header = self.get_header(request)
        if header is None:
            return None

        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None

        # Decode and validate JWT signature
        try:
            validated_token = self.get_validated_token(raw_token)
        except TokenError as e:
            raise InvalidToken(e.args[0])

        # Extract JTI for blacklist verification
        token_jti = validated_token.get("jti")
        if not token_jti:
            raise AuthenticationFailed(
                _("Invalid token: JTI missing."),
                code="token_jti_missing",
            )

        # 1. Blacklist check
        self.blacklist_validator.validate(token_jti)

        # Retrieve user from the database
        user = self.get_user(validated_token)

        # 2. Token version check
        self.version_validator.validate(validated_token.payload, user)

        # 3. Device fingerprint check
        device_fingerprint = get_device_fingerprint(request)
        self.device_validator.validate(validated_token.payload, device_fingerprint)

        # 4. IP check (log only if a change is detected)
        current_ip = get_client_ip(request)
        ip_matches = self.ip_validator.validate(validated_token.payload, current_ip)
        if not ip_matches:
            self._log_ip_change(user, current_ip, request)

        return user, validated_token

    def _log_ip_change(self, user, new_ip: str, request) -> None:
        """
        Logs an IP address change in the security logs.
        Does not block the request but enables monitoring.
        """
        import logging
        security_logger = logging.getLogger("security")
        security_logger.warning(
            f"IP change detected for user [{user.email}]. "
            f"New IP: {new_ip}. "
            f"User-Agent: {request.META.get('HTTP_USER_AGENT', 'unknown')}."
        )