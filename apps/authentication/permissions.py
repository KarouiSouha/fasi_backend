from rest_framework.permissions import BasePermission
from django.contrib.auth import get_user_model

User = get_user_model()


class IsSameUserOrAdmin(BasePermission):
    """
    Allows access if the user is accessing their own data
    or if they are an administrator.

    Used for detailed profile endpoints.
    """
    message = "You can only access your own data."

    def has_object_permission(self, request, view, obj):
        return (
            request.user.is_authenticated
            and (obj.id == request.user.id or request.user.role == User.Role.ADMIN)
        )


class CanManageAgent(BasePermission):
    """
    Checks if a manager has permission to manage a specific agent.
    Managers can only manage agents from their own company.

    Used by AgentDetailView.
    """
    message = "You can only manage agents from your own company."

    def has_object_permission(self, request, view, obj):
        if request.user.role == User.Role.ADMIN:
            return True

        if request.user.role == User.Role.MANAGER:
            return (
                obj.role == User.Role.AGENT
                and obj.company == request.user.company
            )

        return False


class CanResetPassword(BasePermission):
    """
    Checks if a user has permission to reset another user's password.

    Rules:
        - Admin   : can reset anyone's password
        - Manager : can only reset their own agents' passwords
        - Agent   : cannot reset anyone else's password
    """
    message = "You do not have permission to reset this password."

    def has_object_permission(self, request, view, obj):
        if request.user.role == User.Role.ADMIN:
            return True

        if request.user.role == User.Role.MANAGER:
            return (
                obj.role == User.Role.AGENT
                and obj.company == request.user.company
            )

        return False
    