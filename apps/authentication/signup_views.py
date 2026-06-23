"""
apps/authentication/signup_views.py
"""

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import (
    ManagerSignupSerializer,
    ApproveRejectManagerSerializer,
    UserListSerializer,
    RequestPasswordResetSerializer,
    ConfirmPasswordResetSerializer,
)
from .services import EmailService, UserService
from apps.token_security.services import TokenService
from apps.token_security.tokens import TemporaryToken
from rest_framework_simplejwt.exceptions import TokenError

logger = logging.getLogger("django")
User = get_user_model()


class ManagerSignupView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ManagerSignupSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        manager = serializer.save()

        EmailService.send_admin_new_manager_request(manager)

        logger.info(f"[SIGNUP] Manager registered: {manager.email} | Admin notification email sent.")

        return Response(
            {
                "message": "Your account has been successfully created. "
                        "An administrator will review your request. "
                        "You will receive an email as soon as your account is activated.",
                "status": "pending",
            },
            status=status.HTTP_201_CREATED,
        )


class PendingManagersListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_admin:
            return Response(
                {"error": "Access restricted to administrators."},
                status=status.HTTP_403_FORBIDDEN,
            )

        pending = (
            User.objects.filter(
                role=User.Role.MANAGER,
                status=User.AccountStatus.PENDING,
            )
            .select_related("company")
            .order_by("-created_at")
        )

        serializer = UserListSerializer(pending, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ApproveRejectManagerView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, manager_id):
        if not request.user.is_admin:
            return Response(
                {"error": "Access restricted to administrators."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            manager = User.objects.get(id=manager_id, role=User.Role.MANAGER)
        except User.DoesNotExist:
            return Response(
                {"error": "Manager not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ApproveRejectManagerSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        action = serializer.validated_data["action"]
        reason = serializer.validated_data.get("reason", "")

        if action == "approve":
            if manager.status == User.AccountStatus.APPROVED:
                return Response(
                    {"error": "This account is already approved."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            UserService.approve_manager(manager=manager, admin=request.user)
            return Response(
                {
                    "message": f"The account of {manager.full_name} has been approved. An email has been sent.",
                    "user": UserListSerializer(manager).data,
                },
                status=status.HTTP_200_OK,
            )

        else:
            if manager.status == User.AccountStatus.REJECTED:
                return Response(
                    {"error": "This request has already been rejected."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            UserService.reject_manager(manager=manager, admin=request.user, reason=reason)
            return Response(
                {
                    "message": f"The request from {manager.full_name} has been rejected. An email has been sent.",
                    "user": UserListSerializer(manager).data,
                },
                status=status.HTTP_200_OK,
            )


class RequestPasswordResetView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RequestPasswordResetSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        if request.user.is_agent:
            return Response(
                {"error": "You do not have permission to reset this password."},
                status=status.HTTP_403_FORBIDDEN,
            )

        target_user = User.objects.get(id=serializer.validated_data["user_id"])
        UserService.request_password_reset(
            target_user=target_user,
            requesting_user=request.user,
        )

        return Response(
            {"message": f"A password reset link has been sent to {target_user.email}."},
            status=status.HTTP_200_OK,
        )


class ConfirmPasswordResetView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.token_security.tokens import TemporaryToken
        from rest_framework_simplejwt.exceptions import TokenError

        serializer = ConfirmPasswordResetSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            token = TemporaryToken(serializer.validated_data["token"])
            user_id = token["user_id"]
            if token.get("action") != "password_reset":
                raise TokenError("Invalid token for this action.")
        except TokenError as e:
            return Response(
                {"error": f"Invalid or expired token: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"error": "User not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        UserService.reset_password(
            user=user,
            new_password=serializer.validated_data["new_password"],
        )

        return Response(
            {"message": "Password successfully reset. You can now log in."},
            status=status.HTTP_200_OK,
        )