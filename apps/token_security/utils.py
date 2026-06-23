import hashlib


def get_client_ip(request) -> str:
    """
    Retrieves the real client IP address.
    Handles cases where the server is behind a proxy or load balancer
    by reading the X-Forwarded-For header first.
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        # Take only the first IP (the original client IP)
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def get_device_fingerprint(request) -> str:
    """
    Generates a unique device fingerprint from available HTTP headers.
    This fingerprint is consistent for the same browser/device but differs
    across distinct devices.

    Components used:
        - HTTP_USER_AGENT     : browser + operating system
        - HTTP_ACCEPT_LANGUAGE: browser language
        - HTTP_ACCEPT_ENCODING: supported encodings

    Note: Intentionally not 100% unique to respect privacy.
    The goal is detecting device changes, not precise tracking.
    """
    components = [
        request.META.get("HTTP_USER_AGENT", ""),
        request.META.get("HTTP_ACCEPT_LANGUAGE", ""),
        request.META.get("HTTP_ACCEPT_ENCODING", ""),
    ]
    raw_fingerprint = "|".join(components)
    return hashlib.sha256(raw_fingerprint.encode()).hexdigest()


def parse_device_name(user_agent: str) -> str:
    """
    Converts a raw User-Agent string into a human-readable name
    for display in the active sessions list.

    Examples:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120..."
        → "Chrome on Windows"

        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0...) Safari/604..."
        → "Safari on iPhone"
    """
    if not user_agent:
        return "Unknown device"

    ua_lower = user_agent.lower()

    # OS detection
    if "windows" in ua_lower:
        os_name = "Windows"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        os_name = "macOS"
    elif "iphone" in ua_lower:
        os_name = "iPhone"
    elif "ipad" in ua_lower:
        os_name = "iPad"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "linux" in ua_lower:
        os_name = "Linux"
    else:
        os_name = "Unknown device"

    # Browser detection
    if "edg/" in ua_lower:
        browser = "Edge"
    elif "chrome" in ua_lower and "chromium" not in ua_lower:
        browser = "Chrome"
    elif "firefox" in ua_lower:
        browser = "Firefox"
    elif "safari" in ua_lower and "chrome" not in ua_lower:
        browser = "Safari"
    elif "opera" in ua_lower or "opr/" in ua_lower:
        browser = "Opera"
    else:
        browser = "Unknown browser"

    return f"{browser} on {os_name}"