"""
Configuration Django pour l'environnement de production.
Surcharge base.py avec des paramètres de sécurité renforcés.
"""

from .base import *
import dj_database_url
import os
# =============================================================================
# PRODUCTION
# =============================================================================

DEBUG = False
# =============================================================================
# BASE DE DONNÉES — Supabase via DATABASE_URL
# =============================================================================

DATABASES = {
    "default": dj_database_url.config(
        default=os.getenv("DATABASE_URL"),
        conn_max_age=600,
        conn_health_checks=True,
    )
}
# =============================================================================
# CORS — autoriser le frontend Vercel
# =============================================================================

CORS_ALLOWED_ORIGINS = [
    origin.strip().rstrip("/")
    for origin in os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://weeg.vercel.app"
    ).split(",")
]

CORS_ALLOW_CREDENTIALS = True
# =============================================================================
# WHITENOISE — fichiers statiques sans S3
# =============================================================================

MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# =============================================================================
# SÉCURITÉ HTTPS
# =============================================================================

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
X_FRAME_OPTIONS = "DENY"

# =============================================================================
# ALLOWED HOSTS
# =============================================================================

ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS",
    "localhost"
).split(",")