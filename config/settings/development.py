

"""
config/settings/development.py

Paramètres spécifiques à l'environnement de développement.
Hérite de base.py — ne surcharge que ce qui est nécessaire.
"""

from .base import *  # noqa

# =============================================================================
# DEBUG
# =============================================================================

DEBUG = True
ALLOWED_HOSTS = ['*']

# =============================================================================
# BASE DE DONNÉES — même que base.py (PostgreSQL)
# =============================================================================
# Pas de surcharge nécessaire, base.py lit depuis .env

# =============================================================================
# EMAIL
# ⚠️  NE PAS mettre EMAIL_BACKEND ici en dur.
#     base.py le lit déjà depuis .env via env("EMAIL_BACKEND").
#     Si vous le redéfinissez ici, ça écrase la valeur du .env.
# =============================================================================

# ✅ Rien à mettre ici pour l'email — base.py gère tout depuis .env

# =============================================================================
# LOGS — niveau DEBUG en développement
# =============================================================================

LOGGING["loggers"]["django"]["level"] = "DEBUG"  # type: ignore[index]

# =============================================================================
# CORS — permissif en dev
# =============================================================================

CORS_ALLOW_ALL_ORIGINS = True

# =============================================================================
# CACHE — mémoire locale en dev si Redis non disponible
# =============================================================================
# Décommentez si Redis n'est pas lancé :
# CACHES = {
#     "default": {
#         "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
#     }
# }