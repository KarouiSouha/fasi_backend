from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """
    Allows only users with the 'admin' role.
    The role is read directly from the JWT payload without a DB query.

    Used for:
        - Approving / rejecting manager accounts
        - Accessing security logs
        - Managing system configuration
    """
    message = "Access restricted to administrators."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "admin"
        )


class IsManager(BasePermission):
    """
    Allows only users with the 'manager' role.
    The role is read directly from the JWT payload without a DB query.

    Used for:
        - Creating and managing agent accounts
        - Accessing detailed reports
        - Configuring alert thresholds
        - Resetting agent passwords
    """
    message = "Access restricted to managers."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "manager"
        )


class IsAgent(BasePermission):
    """
    Allows only users with the 'agent' role.
    The role is read directly from the JWT payload without a DB query.

    Used for:
        - Viewing data from their branch
        - Importing Excel files
        - Receiving and managing alerts
    """
    message = "Access restricted to agents."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role == "agent"
        )


class IsAdminOrManager(BasePermission):
    """
    Allows users with the 'admin' or 'manager' role.

    Used for:
        - Generating reports
        - Scheduling automatic reports
        - Configuring alerts
        - Resetting passwords
    """
    message = "Access restricted to administrators and managers."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role in ("admin", "manager")
        )


class IsManagerOrAgent(BasePermission):
    """
    Allows users with the 'manager' or 'agent' role.

    Used for resources accessible to all internal users
    except operations reserved for the system administrator.
    """
    message = "Access restricted to managers and agents."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.role in ("manager", "agent")
        )


class HasPermission(BasePermission):
    """
    Granular permission based on the user's permission list.
    The list is read from the JWT payload (field 'permissions').

    Usage in a view:
        permission_classes = [IsAuthenticated, HasPermission]
        required_permission = "view-dashboard"

    Available permissions are defined in the User model.
    """
    message = "You do not have the specific permission required for this action."
    required_permission = None

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        required = getattr(view, "required_permission", self.required_permission)
        if not required:
            return True

        return required in (request.user.permissions_list or [])