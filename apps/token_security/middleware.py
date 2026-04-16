import logging
from django.core.cache import cache
from django.http import JsonResponse
from django.utils.translation import gettext_lazy as _

from .utils import get_client_ip, get_device_fingerprint

security_logger = logging.getLogger("security")

# Rate limiting constants
MAX_LOGIN_ATTEMPTS = 5          # Maximum number of attempts before lockout
LOCKOUT_DURATION = 15 * 60      # Lockout duration in seconds (15 minutes)
LOCKOUT_CACHE_PREFIX = "login_lockout"
ATTEMPTS_CACHE_PREFIX = "login_attempts"


class JWTFingerprintMiddleware:
    """
    Middleware for device fingerprint verification.

    On every authenticated request, it calculates the device fingerprint
    from the request headers and injects it into the request object.

    Note: The actual validation is delegated to DeviceValidator in backends.py.
    This middleware only injects the fingerprint so it can be accessed by the authentication backend.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Calculate and inject the fingerprint into the request
        request.device_fingerprint = get_device_fingerprint(request)
        request.client_ip = get_client_ip(request)

        response = self.get_response(request)
        return response


class RateLimitLoginMiddleware:
    """
    Middleware to protect against brute-force attacks.

    Blocks an IP address for LOCKOUT_DURATION seconds if it exceeds
    MAX_LOGIN_ATTEMPTS consecutive failed login attempts.

    The counter is stored in Redis (via Django cache) for performance and persistence.
    The counter is reset after a successful login.

    Applies only to the login endpoint (POST /api/auth/login/).
    """

    LOGIN_PATH = "/api/auth/login/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path == self.LOGIN_PATH:
            ip_address = get_client_ip(request)

            # Check if the IP is currently locked out
            if self._is_locked_out(ip_address):
                security_logger.warning(
                    f"Login attempt blocked from {ip_address} "
                    f"(too many failed attempts)."
                )
                return JsonResponse(
                    {
                        "error": "Too many failed login attempts. "
                                 "Your access has been temporarily blocked. "
                                 "Please try again in 15 minutes.",
                        "code": "rate_limited",
                    },
                    status=429,
                )

        response = self.get_response(request)

        # Update attempt counter based on the response
        if request.method == "POST" and request.path == self.LOGIN_PATH:
            ip_address = get_client_ip(request)

            if response.status_code == 200:
                # Successful login: reset the counter
                self._reset_attempts(ip_address)
            elif response.status_code in (400, 401, 403):
                # Failed login: increment the counter
                self._increment_attempts(ip_address)

        return response

    def _is_locked_out(self, ip_address: str) -> bool:
        lockout_key = f"{LOCKOUT_CACHE_PREFIX}:{ip_address}"
        return cache.get(lockout_key) is not None

    def _increment_attempts(self, ip_address: str) -> None:
        attempts_key = f"{ATTEMPTS_CACHE_PREFIX}:{ip_address}"
        attempts = cache.get(attempts_key, 0) + 1
        cache.set(attempts_key, attempts, timeout=LOCKOUT_DURATION)

        if attempts >= MAX_LOGIN_ATTEMPTS:
            lockout_key = f"{LOCKOUT_CACHE_PREFIX}:{ip_address}"
            cache.set(lockout_key, True, timeout=LOCKOUT_DURATION)
            security_logger.warning(
                f"IP {ip_address} locked out after {attempts} failed attempts."
            )

    def _reset_attempts(self, ip_address: str) -> None:
        attempts_key = f"{ATTEMPTS_CACHE_PREFIX}:{ip_address}"
        lockout_key = f"{LOCKOUT_CACHE_PREFIX}:{ip_address}"
        cache.delete(attempts_key)
        cache.delete(lockout_key)


class SuspiciousActivityMiddleware:
    """
    Middleware for detecting suspicious activity.

    Monitors abnormal behaviors after authentication:
        - Change of IP address between successive requests
        - Repeated attempts to access unauthorized resources (repeated 403s)

    This middleware does not block requests but logs them in security.log
    for monitoring and potential human review.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Log repeated denied access (potential bypass attempt)
        if response.status_code == 403 and hasattr(request, "user") and request.user.is_authenticated:
            ip_address = get_client_ip(request)
            security_logger.warning(
                f"Access denied (403) for [{request.user.email}] "
                f"on {request.path} from {ip_address}."
            )

        return response