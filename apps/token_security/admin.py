from django.contrib import admin
from django.utils.html import format_html
from .models import RefreshTokenRotation, TokenBlacklist, ActiveSession, LoginAttempt


@admin.register(TokenBlacklist)
class TokenBlacklistAdmin(admin.ModelAdmin):
    list_display = ["token_jti_short", "user", "token_type", "reason", "revoked_at", "expires_at"]
    list_filter = ["token_type", "reason"]
    search_fields = ["user__email", "token_jti"]
    readonly_fields = ["token_jti", "user", "token_type", "revoked_at", "expires_at", "reason"]
    ordering = ["-revoked_at"]

    def token_jti_short(self, obj):
        return obj.token_jti[:20] + "..."
    token_jti_short.short_description = "JTI"

    def has_add_permission(self, request):
        return False


@admin.register(ActiveSession)
class ActiveSessionAdmin(admin.ModelAdmin):
    list_display = ["user", "device_name", "ip_address", "last_activity", "created_at"]
    list_filter = ["created_at"]
    search_fields = ["user__email", "ip_address", "device_name"]
    readonly_fields = [
        "id", "user", "refresh_token_jti", "device_fingerprint",
        "device_name", "ip_address", "last_activity", "created_at",
    ]
    ordering = ["-last_activity"]

    def has_add_permission(self, request):
        return False


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ["email", "ip_address", "status_display", "failure_reason", "attempted_at"]
    list_filter = ["is_successful", "failure_reason", "attempted_at"]
    search_fields = ["email", "ip_address"]
    readonly_fields = [
        "id", "email", "ip_address", "user_agent",
        "is_successful", "failure_reason", "attempted_at",
    ]
    ordering = ["-attempted_at"]

    def status_display(self, obj):
        if obj.is_successful:
            return format_html('<span style="color: green;">✓ Success</span>')
        return format_html('<span style="color: red;">✗ Failed</span>')
    status_display.short_description = "Status"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(RefreshTokenRotation)
class RefreshTokenRotationAdmin(admin.ModelAdmin):
    list_display = ["user", "rotated_at", "ip_address", "device_fingerprint_short"]
    search_fields = ["user__email", "ip_address"]
    readonly_fields = [
        "id", "user", "old_token_jti", "new_token_jti",
        "rotated_at", "ip_address", "device_fingerprint",
    ]
    ordering = ["-rotated_at"]

    def device_fingerprint_short(self, obj):
        if obj.device_fingerprint:
            return obj.device_fingerprint[:16] + "..."
        return "-"
    device_fingerprint_short.short_description = "Fingerprint"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False