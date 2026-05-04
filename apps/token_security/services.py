import logging
from datetime import datetime, timezone

from django.utils.translation import gettext_lazy as _

from .models import TokenBlacklist, ActiveSession, RefreshTokenRotation
from .tokens import CustomAccessToken, CustomRefreshToken, TemporaryToken
from .utils import get_client_ip, get_device_fingerprint, parse_device_name

security_logger = logging.getLogger("security")


class TokenService:
    """
    Couche de service centralisée pour toutes les opérations sur les tokens JWT.

    Cette classe est le seul endroit où les tokens sont créés, rotés, ou révoqués.
    Aucune vue ne doit manipuler directement les tokens sans passer par ce service.
    """

    @staticmethod
    def issue_tokens(user, request) -> dict:
        """
        Génère une paire access + refresh token au moment du login.
        Crée une session active associée à l'appareil et à l'IP.

        Args:
            user    : instance User authentifiée
            request : requête HTTP Django (pour extraire IP et device)

        Returns:
            dict contenant :
                - access  : access token JWT signé
                - refresh : refresh token JWT signé
                - session_id : UUID de la session créée
        """
        ip_address = get_client_ip(request)
        device_fingerprint = get_device_fingerprint(request)
        device_name = parse_device_name(request.META.get("HTTP_USER_AGENT", ""))

        # Génération des tokens enrichis
        refresh_token = CustomRefreshToken.for_user_with_context(
            user=user,
            device_fingerprint=device_fingerprint,
            ip_address=ip_address,
        )
        access_token = refresh_token.access_token

        # Enrichissement de l'access token avec les données de rôle
        access_token["role"] = user.role
        access_token["permissions"] = user.permissions_list
        access_token["token_version"] = user.token_version
        access_token["company_id"] = str(user.company_id) if user.company_id else None

        # Création de la session active en base de données
        session = ActiveSession.objects.create(
            user=user,
            refresh_token_jti=refresh_token["jti"],
            device_fingerprint=device_fingerprint,
            device_name=device_name,
            ip_address=ip_address,
        )

        security_logger.info(
            f"Nouvelle session créée pour [{user.email}] "
            f"depuis {ip_address} sur {device_name}."
        )

        return {
            "access": str(access_token),
            "refresh": str(refresh_token),
            "session_id": str(session.id),
        }

    @staticmethod
    def rotate_refresh_token(old_refresh_token_payload: dict, request) -> dict:
        """
        Effectue la rotation d'un refresh token :
            1. Vérifie que l'ancien refresh token n'a pas déjà été utilisé (token reuse attack)
            2. Blackliste l'ancien token
            3. Génère un nouveau refresh token
            4. Met à jour la session active

        Args:
            old_refresh_token_payload : payload décodé de l'ancien refresh token
            request                   : requête HTTP courante

        Returns:
            dict contenant le nouvel access token et le nouveau refresh token.

        Raises:
            ValueError si l'ancien JTI a déjà été utilisé (attaque détectée).
        """
        from django.contrib.auth import get_user_model
        User = get_user_model()

        old_jti = old_refresh_token_payload["jti"]
        user_id = old_refresh_token_payload["user_id"]

        # Détection d'une tentative de réutilisation d'un ancien refresh token
        if RefreshTokenRotation.objects.filter(old_token_jti=old_jti).exists():
            # Révocation immédiate de TOUS les tokens de l'utilisateur
            user = User.objects.get(id=user_id)
            TokenService.revoke_all_user_tokens(user, reason="token_reuse")

            security_logger.critical(
                f"ATTAQUE DÉTECTÉE : réutilisation d'un ancien refresh token "
                f"pour l'utilisateur [{user.email}]. "
                f"Tous ses tokens ont été révoqués."
            )
            raise ValueError("Refresh token déjà utilisé. Tous vos tokens ont été révoqués par mesure de sécurité.")

        user = User.objects.get(id=user_id)
        ip_address = get_client_ip(request)
        device_fingerprint = get_device_fingerprint(request)

        # Génération du nouveau refresh token
        new_refresh_token = CustomRefreshToken.for_user_with_context(
            user=user,
            device_fingerprint=device_fingerprint,
            ip_address=ip_address,
        )
        new_access_token = new_refresh_token.access_token
        new_access_token["role"] = user.role
        new_access_token["permissions"] = user.permissions_list
        new_access_token["token_version"] = user.token_version
        new_access_token["company_id"] = str(user.company_id) if user.company_id else None

        # Enregistrement de la rotation
        RefreshTokenRotation.objects.create(
            user=user,
            old_token_jti=old_jti,
            new_token_jti=new_refresh_token["jti"],
            ip_address=ip_address,
            device_fingerprint=device_fingerprint,
        )

        # Blacklist de l'ancien refresh token
        TokenService._blacklist_jti(
            jti=old_jti,
            user=user,
            token_type="refresh",
            reason="logout",
        )

        # Mise à jour de la session active avec le nouveau JTI
        ActiveSession.objects.filter(
            user=user,
            refresh_token_jti=old_jti,
        ).update(refresh_token_jti=new_refresh_token["jti"])

        return {
            "access": str(new_access_token),
            "refresh": str(new_refresh_token),
        }

    @staticmethod
    def revoke_token(token_jti: str, user, token_type: str = "refresh", reason: str = "logout") -> None:
        """
        Révoque un token spécifique et supprime la session associée.

        Args:
            token_jti  : identifiant unique du token à révoquer
            user       : propriétaire du token
            token_type : "access" | "refresh" | "temporary"
            reason     : motif de révocation
        """
        TokenService._blacklist_jti(
            jti=token_jti,
            user=user,
            token_type=token_type,
            reason=reason,
        )

        # Suppression de la session active liée à ce refresh token
        if token_type == "refresh":
            ActiveSession.objects.filter(
                user=user,
                refresh_token_jti=token_jti,
            ).delete()

        security_logger.info(
            f"Token révoqué pour [{user.email}] - Raison : {reason}."
        )

    @staticmethod
    def revoke_all_user_tokens(user, reason: str = "logout_all") -> int:
        """
        Révoque toutes les sessions actives d'un utilisateur.
        Utilisé lors du logout global ou après détection d'activité suspecte.

        Args:
            user   : utilisateur dont tous les tokens sont révoqués
            reason : motif de révocation

        Returns:
            Nombre de sessions révoquées.
        """
        # Invalidate all existing access tokens immediately by bumping the user's token version.
        # This forces every access token already issued to become invalid.
        user.token_version += 1
        user.save(update_fields=["token_version"])

        active_sessions = ActiveSession.objects.filter(user=user)
        count = active_sessions.count()

        for session in active_sessions:
            TokenService._blacklist_jti(
                jti=session.refresh_token_jti,
                user=user,
                token_type="refresh",
                reason=reason,
            )

        active_sessions.delete()

        security_logger.warning(
            f"Toutes les sessions révoquées pour [{user.email}] "
            f"({count} sessions). Raison : {reason}."
        )

        return count

    @staticmethod
    def get_active_sessions(user) -> list:
        """
        Retourne la liste des sessions actives de l'utilisateur.

        Returns:
            Liste de dicts contenant les informations de chaque session.
        """
        sessions = ActiveSession.objects.filter(user=user).order_by("-last_activity")
        return [
            {
                "session_id": str(session.id),
                "device_name": session.device_name or "Appareil inconnu",
                "ip_address": session.ip_address,
                "last_activity": session.last_activity,
                "created_at": session.created_at,
                "is_current": session.is_current,
            }
            for session in sessions
        ]

    @staticmethod
    def issue_temporary_token(user, action: str) -> str:
        """
        Génère un token temporaire pour le reset de mot de passe ou la vérification d'email.

        Args:
            user   : utilisateur concerné
            action : "password_reset" | "email_verification"

        Returns:
            Token temporaire sous forme de chaîne signée.
        """
        token = TemporaryToken.for_user_action(user=user, action=action)
        return str(token)

    @staticmethod
    def _blacklist_jti(jti: str, user, token_type: str, reason: str) -> None:
        """
        Méthode interne : ajoute un JTI à la blacklist.
        Évite les doublons grâce à get_or_create.
        """
        from django.utils import timezone as tz
        from datetime import timedelta

        # Calcul de la date d'expiration selon le type de token
        lifetimes = {
            "access": timedelta(minutes=60),
            "refresh": timedelta(days=7),
            "temporary": timedelta(hours=1),
        }
        expires_at = tz.now() + lifetimes.get(token_type, timedelta(days=1))

        TokenBlacklist.objects.get_or_create(
            token_jti=jti,
            defaults={
                "user": user,
                "token_type": token_type,
                "expires_at": expires_at,
                "reason": reason,
            },
        )
