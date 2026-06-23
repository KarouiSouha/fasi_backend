from django.urls import path
from .views import (
    ProfileView,
    ChangePasswordView,
    CreateAgentView,
    AgentListView,
    AgentDetailView,
    UpdateUserPermissionsView,
    UpdateUserStatusView,
    AllUsersListView,
)
from .signup_views import (
    ManagerSignupView,
    PendingManagersListView,
    ApproveRejectManagerView,
    RequestPasswordResetView,
    ConfirmPasswordResetView,
)
from .forgot_password_views import (
    ForgotPasswordRequestView,
    ForgotPasswordVerifyView,
    ForgotPasswordResetView,
)
# NEW: email verification for manager signup
from .email_verification_views import (
    VerifyManagerEmailView,
    ResendVerificationEmailView,
)

app_name = "authentication"

urlpatterns = [
    # -------------------------------------------------------------------------
    # Signup & Manager validation
    # -------------------------------------------------------------------------
    path("signup/",                              ManagerSignupView.as_view(),        name="manager-signup"),
    path("signup/pending/",                      PendingManagersListView.as_view(),  name="pending-managers"),
    path("signup/review/<uuid:manager_id>/",     ApproveRejectManagerView.as_view(), name="review-manager"),

    # ── Email verification (NEW) ──────────────────────────────────────────────
    # Step triggered by the link in the verification email
    path("verify-email/",                        VerifyManagerEmailView.as_view(),        name="verify-email"),
    # Resend verification email if expired / not received
    path("resend-verification/",                 ResendVerificationEmailView.as_view(),   name="resend-verification"),

    # -------------------------------------------------------------------------
    # Profile & password
    # -------------------------------------------------------------------------
    path("profile/",                             ProfileView.as_view(),              name="profile"),
    path("change-password/",                     ChangePasswordView.as_view(),       name="change-password"),
    path("password-reset/request/",              RequestPasswordResetView.as_view(), name="password-reset-request"),
    path("password-reset/confirm/",              ConfirmPasswordResetView.as_view(), name="password-reset-confirm"),

    # -------------------------------------------------------------------------
    # Forgot password (code by email — 3 steps)
    # -------------------------------------------------------------------------
    path("forgot-password/request/",             ForgotPasswordRequestView.as_view(), name="forgot-password-request"),
    path("forgot-password/verify/",              ForgotPasswordVerifyView.as_view(),  name="forgot-password-verify"),
    path("forgot-password/reset/",               ForgotPasswordResetView.as_view(),   name="forgot-password-reset"),

    # -------------------------------------------------------------------------
    # Agents (manager)
    # -------------------------------------------------------------------------
    path("agents/",                              AgentListView.as_view(),            name="agent-list"),
    path("agents/create/",                       CreateAgentView.as_view(),          name="agent-create"),
    path("agents/<uuid:agent_id>/",              AgentDetailView.as_view(),          name="agent-detail"),

    # -------------------------------------------------------------------------
    # User management (admin)
    # -------------------------------------------------------------------------
    path("users/",                               AllUsersListView.as_view(),         name="user-list"),
    path("users/<uuid:user_id>/permissions/",    UpdateUserPermissionsView.as_view(),name="user-permissions"),
    path("users/<uuid:user_id>/status/",         UpdateUserStatusView.as_view(),     name="user-status"),
]