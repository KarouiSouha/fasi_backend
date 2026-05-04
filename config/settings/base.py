"""
Configuration Django de base — commune à tous les environnements.
Les paramètres spécifiques à dev/prod sont dans development.py et production.py.
"""

import os
from pathlib import Path
from datetime import timedelta
import environ

# =============================================================================
# CHEMINS
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Chargement des variables d'environnement depuis .env
env = environ.Env()
environ.Env.read_env(os.path.join(BASE_DIR, ".env"))

# =============================================================================
# SÉCURITÉ
# =============================================================================

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# =============================================================================
# APPLICATIONS INSTALLÉES
# =============================================================================

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
]

LOCAL_APPS = [
    "apps.branches",           # ← branches AVANT authentication (FK)
    "apps.companies",
    "apps.authentication",
    "apps.token_security",
    "apps.products",
    "apps.customers",
    "apps.transactions",
    "apps.inventory",
    "apps.kpi",
    "apps.alerts",
    "apps.aging",
    "apps.ai_insights",
    "apps.data_import",
    "apps.notifications",
    
    
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# =============================================================================
# MIDDLEWARE
# =============================================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",                              # CORS
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.token_security.middleware.JWTFingerprintMiddleware",           # JWT fingerprint
    "apps.token_security.middleware.RateLimitLoginMiddleware",           # Rate limiting
    "apps.token_security.middleware.SuspiciousActivityMiddleware",       # Activité suspecte
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# =============================================================================
# URLS ET WSGI
# =============================================================================

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# =============================================================================
# TEMPLATES
# =============================================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# =============================================================================
# BASE DE DONNÉES PostgreSQL
# =============================================================================

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="fasi_db"),
        "USER": env("DB_USER", default="fasi_user"),
        "PASSWORD": env("DB_PASSWORD", default=""),
        "HOST": env("DB_HOST", default="localhost"),
        "PORT": env("DB_PORT", default="5432"),
        "OPTIONS": {
            "connect_timeout": 10,
            "options": f"-c search_path={env('DB_SCHEMA', default=env('DB_USER', default='public'))},public",
        },
    }
}

# =============================================================================
# MODÈLE UTILISATEUR CUSTOM
# =============================================================================

AUTH_USER_MODEL = "authentication.User"

# Backend d'authentification : email à la place du username
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]

# =============================================================================
# DJANGO REST FRAMEWORK
# =============================================================================

REST_FRAMEWORK = {
    # Notre backend JWT custom à la place de simplejwt par défaut
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.token_security.backends.CustomJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "core.exceptions.custom_exception_handler",
}

# =============================================================================
# JWT (djangorestframework-simplejwt)
# =============================================================================

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("ACCESS_TOKEN_LIFETIME", default=60)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("REFRESH_TOKEN_LIFETIME", default=7)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("SECRET_KEY"),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "TOKEN_TYPE_CLAIM": "token_type",
    "JTI_CLAIM": "jti",
    # On utilise nos tokens custom
    "ACCESS_TOKEN_CLASS": "apps.token_security.tokens.CustomAccessToken",
    "REFRESH_TOKEN_CLASS": "apps.token_security.tokens.CustomRefreshToken",
}

# =============================================================================
# CORS
# =============================================================================

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=[
        "http://localhost:4000",   # vite dev server alternatif
        "http://localhost:5173",   # Vite dev server
        "http://localhost:3000",   # React dev server alternatif
    ],
)
CORS_ALLOW_CREDENTIALS = True

# =============================================================================
# CACHE (Redis)
# =============================================================================

# CACHES = {
#     "default": {
#         "BACKEND": "django.core.cache.backends.redis.RedisCache",
#         "LOCATION": env("REDIS_URL", default="redis://localhost:6379/0"),
#     }
# }

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
# =============================================================================
# CELERY
# =============================================================================

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "Africa/Tunis"

# =============================================================================
# EMAIL
# =============================================================================

EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=465)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=True)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default=" weeg@digitalia.ly")
EMAIL_TIMEOUT = 10
# URL du frontend (utilisé dans les liens des emails)
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173")

# =============================================================================
# FICHIERS STATIQUES ET MÉDIAS
# =============================================================================

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# =============================================================================
# INTERNATIONALISATION
# =============================================================================

LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Africa/Tunis"
USE_I18N = True
USE_TZ = True

# =============================================================================
# VALIDATION DES MOTS DE PASSE
# =============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# =============================================================================
# LOGS
# =============================================================================
# =============================================================================
# LOGS
# =============================================================================

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
            'level': 'WARNING',  # Seulement WARNING et plus
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',  # Niveau global
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',  # Django seulement WARNING+
            'propagate': False,
        },
        'django.utils.autoreload': {
            'handlers': ['console'],
            'level': 'ERROR',  # Autoreload seulement ERROR+
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console'],
            'level': 'INFO',  # Garder les infos du serveur (démarrage, requêtes)
            'propagate': False,
        },
    },
}
# =============================================================================
# SWAGGER / OpenAPI (drf-spectacular)
# =============================================================================

SPECTACULAR_SETTINGS = {
    "TITLE": "WEEG API",
    "DESCRIPTION": "API Backend — Plateforme SaaS de gestion commerciale WEEG",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# =============================================================================
# DIVERS
# =============================================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# =============================================================================
# OpenAI / AI RAG
# =============================================================================

OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
AI_MODEL_SMART = env("AI_MODEL_SMART", default="gpt-4o")
AI_MODEL_FAST  = env("AI_MODEL_FAST",  default="gpt-4o-mini")
OPENAI_EMBEDDING_MODEL = env("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")
AI_RAG_MAX_RESPONSE_TOKENS = env.int("AI_RAG_MAX_RESPONSE_TOKENS", default=600)
AI_RAG_ENABLED = env.bool("AI_RAG_ENABLED", default=True)

# Qdrant vector store
QDRANT_URL = env("QDRANT_URL", default="")
QDRANT_API_KEY = env("QDRANT_API_KEY", default="")
QDRANT_COLLECTION_NAME = env("QDRANT_COLLECTION_NAME", default="weeg_documents")
QDRANT_DISTANCE = env("QDRANT_DISTANCE", default="cosine")
OPENAI_EMBEDDING_DIM = env.int("OPENAI_EMBEDDING_DIM", default=1536)

# ── Text-to-SQL ───────────────────────────────────────────────────────────────
AI_TEXT_TO_SQL_ENABLED = True       # Activer/désactiver le feature
AI_TEXT_TO_SQL_TIMEOUT = 10_000     # Timeout PostgreSQL (ms)
AI_TEXT_TO_SQL_MAX_ROWS = 500       # Lignes max retournées
AI_TEXT_TO_SQL_LOG_SQL = True       # Logger les requêtes générées (audit)

# =============================================================================
# Email Verification API (Abstract Email Validation)
# =============================================================================

ABSTRACT_API_KEY = env("ABSTRACT_API_KEY", default="")