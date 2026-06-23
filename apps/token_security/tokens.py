import hashlib
from datetime import timedelta
from django.conf import settings
from rest_framework_simplejwt.tokens import Token, AccessToken, RefreshToken


class CustomAccessToken(AccessToken):
    """
    Access token enriched with additional information in the payload.
    This avoids extra DB queries on every permission check.

    Enriched payload:
        - user_id       : unique user identifier
        - role          : admin | manager | agent
        - permissions   : list of granted permissions
        - company_id    : assigned company identifier
        - token_version : version to invalidate all tokens after a password change
        - device_fp     : hashed device fingerprint
        - ip_hash       : hashed IP address at login time
    """
    token_type = "access"
    lifetime = timedelta(minutes=60)

    @classmethod
    def for_user_with_context(cls, user, device_fingerprint: str, ip_address: str):
        """
        Generates an enriched access token for the given user.
        Should only be called via TokenService.issue_tokens().
        """
        token = cls.for_user(user)

        # Role and permissions information
        token["role"] = user.role
        token["permissions"] = user.permissions_list
        token["token_version"] = user.token_version

        # Company information (None if user has no assigned company)
        token["company_id"] = str(user.company_id) if user.company_id else None

        # Security fingerprint (hashed to avoid exposing raw data)
        token["device_fp"] = cls._hash_value(device_fingerprint)
        token["ip_hash"] = cls._hash_value(ip_address)

        return token

    @staticmethod
    def _hash_value(value: str) -> str:
        """Hashes a value with SHA-256 for secure storage in the payload."""
        return hashlib.sha256(value.encode()).hexdigest()


class CustomRefreshToken(RefreshToken):
    """
    Refresh token with automatic rotation support.
    On each use, the old token is revoked and a new one is generated.
    Detecting reuse of an old refresh token triggers
    revocation of ALL the user's tokens (token reuse attack).
    """
    token_type = "refresh"
    lifetime = timedelta(days=7)
    access_token_class = CustomAccessToken

    @classmethod
    def for_user_with_context(cls, user, device_fingerprint: str, ip_address: str):
        """
        Generates a refresh token tied to the login context.
        The device_fingerprint and ip_address are stored for later validation.
        """
        token = cls.for_user(user)
        token["device_fp"] = CustomAccessToken._hash_value(device_fingerprint)
        token["ip_hash"] = CustomAccessToken._hash_value(ip_address)
        token["token_version"] = user.token_version
        return token


class TemporaryToken(Token):
    """
    Single-use, short-lived token.
    Used exclusively for:
        - Password reset
        - Email verification during manager registration

    This token is NOT used for API request authentication.
    It is transmitted via a link in an email.
    Once used, it is immediately blacklisted.
    """
    token_type = "temporary"
    lifetime = timedelta(hours=1)

    @classmethod
    def for_user_action(cls, user, action: str):
        """
        Generates a temporary token for a specific action.

        Args:
            user    : the user concerned
            action  : "password_reset" | "email_verification"

        Returns:
            TemporaryToken with the action context encoded in the payload.
        """
        token = cls()
        token["user_id"] = str(user.id)
        token["email"] = user.email
        token["action"] = action
        token["token_version"] = user.token_version
        return token