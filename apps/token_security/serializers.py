from rest_framework import serializers
from .models import ActiveSession, LoginAttempt


class ActiveSessionSerializer(serializers.ModelSerializer):
    """
    Serializes an active session for display in the connected devices list.
    Used by ActiveSessionsView and RevokeSessionView.
    """

    class Meta:
        model = ActiveSession
        fields = [
            "id",
            "device_name",
            "ip_address",
            "last_activity",
            "created_at",
            "is_current",
        ]
        read_only_fields = fields


class LoginAttemptSerializer(serializers.ModelSerializer):
    """
    Serializes a login attempt for display in the admin panel.
    Read-only, used for consultation only.
    """

    class Meta:
        model = LoginAttempt
        fields = [
            "id",
            "email",
            "ip_address",
            "is_successful",
            "failure_reason",
            "attempted_at",
        ]
        read_only_fields = fields


class TokenRefreshInputSerializer(serializers.Serializer):
    """
    Validates the request body for refresh token rotation.
    """
    refresh = serializers.CharField(
        required=True,
        help_text="JWT refresh token to renew.",
    )


class RevokeSessionInputSerializer(serializers.Serializer):
    """
    Validates the request body for remote session revocation.
    """
    session_id = serializers.UUIDField(
        required=True,
        help_text="UUID of the active session to revoke.",
    )