from django.apps import AppConfig


class TokenSecurityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.token_security"
    verbose_name = "JWT Security"