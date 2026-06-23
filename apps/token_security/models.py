import uuid
from django.db import models
from django.conf import settings


class RefreshTokenRotation(models.Model):
    """
    Records each refresh token rotation.
    Allows detection of refresh token reuse attacks.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="refresh_token_rotations",
    )
    old_token_jti = models.CharField(
        max_length=255,
        unique=True,
        help_text="JTI (JWT ID) of the old refresh token revoked during rotation.",
    )
    new_token_jti = models.CharField(
        max_length=255,
        help_text="JTI of the new refresh token generated after rotation.",
    )
    rotated_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_fingerprint = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "token_refresh_rotation"
        ordering = ["-rotated_at"]
        verbose_name = "Refresh Token Rotation"
        verbose_name_plural = "Refresh Token Rotations"

    def __str__(self):
        return f"Rotation [{self.user.email}] at {self.rotated_at}"


class TokenBlacklist(models.Model):
    """
    Stores revoked tokens (logout, logout-all, password change).
    Checked on every request via CustomJWTAuthentication.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_jti = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique JWT ID of the revoked token.",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="blacklisted_tokens",
    )
    token_type = models.CharField(
        max_length=20,
        choices=[
            ("access", "Access Token"),
            ("refresh", "Refresh Token"),
            ("temporary", "Temporary Token"),
        ],
        default="refresh",
    )
    revoked_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        help_text="Token expiry date. Used for automatic cleanup via Celery.",
    )
    reason = models.CharField(
        max_length=100,
        choices=[
            ("logout", "Logout"),
            ("logout_all", "Logout from all devices"),
            ("password_changed", "Password changed"),
            ("password_reset", "Password reset"),
            ("admin_revoked", "Revoked by administrator"),
            ("suspicious_activity", "Suspicious activity detected"),
            ("token_reuse", "Old token reused"),
        ],
        default="logout",
    )

    class Meta:
        db_table = "token_blacklist"
        ordering = ["-revoked_at"]
        verbose_name = "Blacklisted Token"
        verbose_name_plural = "Blacklisted Tokens"

    def __str__(self):
        return f"Blacklist [{self.token_type}] {self.user.email} - {self.reason}"


class ActiveSession(models.Model):
    """
    Represents an active session: a user logged in on a specific device.
    Created at login, deleted at logout.
    Allows the user to view and remotely revoke their sessions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="active_sessions",
    )
    refresh_token_jti = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="JTI of the refresh token linked to this session.",
    )
    device_fingerprint = models.CharField(
        max_length=255,
        help_text="Hashed fingerprint of the device (User-Agent + other data).",
    )
    device_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Human-readable device name (e.g. Chrome on Windows).",
    )
    ip_address = models.GenericIPAddressField(
        help_text="IP address at login time.",
    )
    last_activity = models.DateTimeField(
        auto_now=True,
        help_text="Last activity detected on this session.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(
        default=False,
        help_text="Indicates whether this is the active session for the current request.",
    )

    class Meta:
        db_table = "token_active_session"
        ordering = ["-last_activity"]
        verbose_name = "Active Session"
        verbose_name_plural = "Active Sessions"

    def __str__(self):
        return f"Session [{self.user.email}] - {self.device_name or self.ip_address}"


class LoginAttempt(models.Model):
    """
    History of all login attempts (successful or failed).
    Used by RateLimitLoginMiddleware to block brute-force attacks.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(
        db_index=True,
        help_text="Email used during the attempt (even if the user does not exist).",
    )
    ip_address = models.GenericIPAddressField(db_index=True)
    user_agent = models.TextField(null=True, blank=True)
    is_successful = models.BooleanField(default=False)
    failure_reason = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        choices=[
            ("invalid_credentials", "Invalid credentials"),
            ("account_pending", "Account pending approval"),
            ("account_rejected", "Account rejected"),
            ("account_suspended", "Account suspended"),
            ("rate_limited", "Too many attempts"),
        ],
    )
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "token_login_attempt"
        ordering = ["-attempted_at"]
        verbose_name = "Login Attempt"
        verbose_name_plural = "Login Attempts"

    def __str__(self):
        status = "Success" if self.is_successful else f"Failed ({self.failure_reason})"
        return f"[{status}] {self.email} from {self.ip_address}"