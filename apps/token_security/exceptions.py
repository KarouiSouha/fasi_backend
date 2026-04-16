from rest_framework.exceptions import APIException
from rest_framework import status


class TokenExpiredException(APIException):
    """
    Raised when a JWT token has exceeded its lifetime.
    The frontend should redirect to the refresh endpoint or the login page.
    """
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Your session has expired. Please log in again."
    default_code = "token_expired"


class TokenBlacklistedException(APIException):
    """
    Raised when a revoked token is reused after logout.
    Indicates an attempt to access the system with an invalid token.
    """
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "This token has been revoked. Please log in again."
    default_code = "token_blacklisted"


class InvalidTokenVersionException(APIException):
    """
    Raised when the token version no longer matches the user's current version.
    Triggered after a password change or reset.
    All old tokens are automatically invalidated.
    """
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Your session is invalid due to a password change. Please log in again."
    default_code = "token_version_mismatch"


class SuspiciousDeviceException(APIException):
    """
    Raised when the device fingerprint does not match the one stored in the token payload.
    Possible attempt to use a stolen token from another device.
    """
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Unrecognized device. Session invalidated for security reasons."
    default_code = "suspicious_device"


class TooManyLoginAttemptsException(APIException):
    """
    Raised by RateLimitLoginMiddleware when an IP exceeds the maximum
    number of allowed login attempts.
    """
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "Too many login attempts. Your access has been temporarily blocked. Please try again in 15 minutes."
    default_code = "rate_limited"


class AccountPendingException(APIException):
    """
    Raised when a manager tries to log in before their account has been approved by an admin.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Your account is pending approval by an administrator."
    default_code = "account_pending"


class AccountRejectedException(APIException):
    """
    Raised when a manager tries to log in after their access request has been rejected by an admin.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Your access request has been rejected. Please contact an administrator."
    default_code = "account_rejected"


class AccountSuspendedException(APIException):
    """
    Raised when a user tries to log in but their account has been manually suspended by an admin or manager.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "Your account has been suspended. Please contact an administrator."
    default_code = "account_suspended"


class PermissionDeniedException(APIException):
    """
    Raised when a user attempts to access a resource for which they do not have the necessary permissions.
    """
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "You do not have the necessary permissions to perform this action."
    default_code = "permission_denied"


class TokenReuseDetectedException(APIException):
    """
    Raised when an old refresh token (already rotated) is reused.
    Indicates a potential attack. All user tokens are immediately revoked.
    """
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Suspicious activity detected. All your sessions have been terminated for security reasons."
    default_code = "token_reuse_detected"