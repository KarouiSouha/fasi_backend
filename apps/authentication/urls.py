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
# NEW: Forgot password with verification code
from .forgot_password_views import (
    ForgotPasswordRequestView,
    ForgotPasswordVerifyView,
    ForgotPasswordResetView,
)

app_name = "authentication"

urlpatterns = [
    # -------------------------------------------------------------------------
    # Signup & validation Manager
    # -------------------------------------------------------------------------
    path("signup/",                              ManagerSignupView.as_view(),        name="manager-signup"),
    path("signup/pending/",                      PendingManagersListView.as_view(),  name="pending-managers"),
    path("signup/review/<uuid:manager_id>/",     ApproveRejectManagerView.as_view(), name="review-manager"),

    # -------------------------------------------------------------------------
    # Profil & mot de passe
    # -------------------------------------------------------------------------
    path("profile/",                             ProfileView.as_view(),              name="profile"),
    path("change-password/",                     ChangePasswordView.as_view(),       name="change-password"),
    path("password-reset/request/",              RequestPasswordResetView.as_view(), name="password-reset-request"),
    path("password-reset/confirm/",              ConfirmPasswordResetView.as_view(), name="password-reset-confirm"),
    # -------------------------------------------------------------------------
    # NEW: Forgot password (auto-service par code email — pour manager/agent)
    # -------------------------------------------------------------------------
    # Step 1 : L'utilisateur entre son email → reçoit un code 6 chiffres
    path("forgot-password/request/",             ForgotPasswordRequestView.as_view(), name="forgot-password-request"),
    # Step 2 : L'utilisateur vérifie son code → reçoit un token temporaire
    path("forgot-password/verify/",              ForgotPasswordVerifyView.as_view(),  name="forgot-password-verify"),
    # Step 3 : L'utilisateur soumet son nouveau mot de passe avec le token
    path("forgot-password/reset/",               ForgotPasswordResetView.as_view(),   name="forgot-password-reset"),
    # -------------------------------------------------------------------------
    # Agents (manager)
    # -------------------------------------------------------------------------
    path("agents/",                              AgentListView.as_view(),            name="agent-list"),
    path("agents/create/",                       CreateAgentView.as_view(),          name="agent-create"),
    path("agents/<uuid:agent_id>/",              AgentDetailView.as_view(),          name="agent-detail"),

    # -------------------------------------------------------------------------
    # Gestion utilisateurs (admin)
    # -------------------------------------------------------------------------
    path("users/",                               AllUsersListView.as_view(),         name="user-list"),
    path("users/<uuid:user_id>/permissions/",    UpdateUserPermissionsView.as_view(),name="user-permissions"),
    path("users/<uuid:user_id>/status/",         UpdateUserStatusView.as_view(),     name="user-status"),
    
]