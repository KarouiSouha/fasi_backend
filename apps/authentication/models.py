import uuid
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """
    Custom manager for the User model.
    Uses email instead of username for authentication.
    """

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The email address is required.")
        email = self.normalize_email(email)
        extra_fields.setdefault("is_active", True)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "admin")
        extra_fields.setdefault("status", "active")
        extra_fields.setdefault("is_verified", True)
        extra_fields.setdefault("first_name", "Admin")
        extra_fields.setdefault("last_name", "WEEG")
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Custom user model that replaces Django's default User.
    Configured in settings/base.py with: AUTH_USER_MODEL = 'authentication.User'

    Three possible roles:
        - admin   : created manually via createsuperuser, manages managers
        - manager : registers via public form, awaits admin approval, manages agents
        - agent   : created by a manager, can log in immediately

    Account status lifecycle:
        PENDING  → APPROVED  (admin approves)
        PENDING  → REJECTED  (admin rejects)
        APPROVED → ACTIVE    (after first successful login)
        ACTIVE   → SUSPENDED (admin or manager suspends)

    Company relationship:
        - Admin  : company = NULL
        - Manager: company = required (provided at registration)
        - Agent  : company = automatically inherited from the creating manager
    """

    # -------------------------------------------------------------------------
    # Custom manager
    # -------------------------------------------------------------------------

    objects = UserManager()

    # -------------------------------------------------------------------------
    # Choice definitions
    # -------------------------------------------------------------------------

    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        MANAGER = "manager", "Manager"
        AGENT = "agent", "Agent"

    class AccountStatus(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"

    # -------------------------------------------------------------------------
    # Main fields
    # -------------------------------------------------------------------------

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # Email is used as the login identifier instead of username
    email = models.EmailField(
        unique=True,
        verbose_name="Email address",
    )

    username = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Username",
        help_text="Optional field. Email is used for login.",
    )

    first_name = models.CharField(max_length=150, verbose_name="First name")
    last_name = models.CharField(max_length=150, verbose_name="Last name")

    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name="Phone number",
    )

    # -------------------------------------------------------------------------
    # Role and status
    # -------------------------------------------------------------------------

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.AGENT,
        verbose_name="Role",
        db_index=True,
    )

    status = models.CharField(
        max_length=20,
        choices=AccountStatus.choices,
        default=AccountStatus.PENDING,
        verbose_name="Account status",
        db_index=True,
    )

    # -------------------------------------------------------------------------
    # Granular permissions
    # -------------------------------------------------------------------------

    permissions_list = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Permissions list",
        help_text=(
            "List of permissions granted to this user. "
            "Stored in the JWT payload to avoid DB queries on every request. "
            "Examples: ['view-dashboard', 'export-reports', 'manage-alerts']"
        ),
    )

    # -------------------------------------------------------------------------
    # Company association
    # -------------------------------------------------------------------------

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="Company",
        help_text="Company the user belongs to. Null for admins .",
    )

    # -------------------------------------------------------------------------
    # Advanced JWT security
    # -------------------------------------------------------------------------

    token_version = models.PositiveIntegerField(
        default=0,
        verbose_name="Token version",
        help_text=(
            "Incremented on every password change or reset. "
            "All tokens with a lower version are automatically invalidated."
        ),
    )

    # -------------------------------------------------------------------------
    # Special flags
    # -------------------------------------------------------------------------

    must_change_password = models.BooleanField(
        default=False,
        verbose_name="Must change password",
        help_text=(
            "Set to True when creating an agent account by a manager. "
            "Forces the agent to change the temporary password on first login."
        ),
    )

    is_verified = models.BooleanField(
        default=False,
        verbose_name="Email verified",
        help_text="True after admin verifies the email (for managers).",
    )
    is_email_verified = models.BooleanField(
        default=False,
        verbose_name="Email address verified",
        help_text=(
            "True after the manager clicks the verification link in their inbox. "
            "Required BEFORE the admin review step. "
            "Not to be confused with is_verified (set by admin on approval)."
         ),
        )
    
    # -------------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------------

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Created at")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Last modified")

    # Reason for rejection or suspension (provided by admin or manager)
    rejection_reason = models.TextField(
        blank=True,
        null=True,
        verbose_name="Rejection / suspension reason",
        help_text="Provided by admin when rejecting or suspending the account.",
    )

    # Who created this account (manager creating an agent, admin creating a manager)
    created_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_users",
        verbose_name="Created by",
    )

    # -------------------------------------------------------------------------
    # Django auth configuration
    # -------------------------------------------------------------------------

    # Use email instead of username for login
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        db_table = "auth_user"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_full_name()} ({self.email}) - {self.role}"

    # -------------------------------------------------------------------------
    # Utility properties
    # -------------------------------------------------------------------------

    @property
    def full_name(self) -> str:
        """Returns the user's full name."""
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def company_name(self) -> str | None:
        """Returns the company name or None."""
        return self.company.name if self.company else None

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role == self.Role.MANAGER

    @property
    def is_agent(self) -> bool:
        return self.role == self.Role.AGENT

    @property
    def is_active_account(self) -> bool:
        """True if the account can log in (approved or active)."""
        return self.status in (self.AccountStatus.APPROVED, self.AccountStatus.ACTIVE)

    # -------------------------------------------------------------------------
    # Business methods
    # -------------------------------------------------------------------------

    def increment_token_version(self) -> None:
        """
        Increments the token version to invalidate all existing tokens.
        Called on password change or reset.
        """
        self.token_version += 1
        self.save(update_fields=["token_version", "updated_at"])

    def approve(self) -> None:
        """Approves a manager account."""
        self.status = self.AccountStatus.APPROVED
        self.is_verified = True
        self.save(update_fields=["status", "is_verified", "updated_at"])

    def reject(self, reason: str = "") -> None:
        """Rejects a manager access request."""
        self.status = self.AccountStatus.REJECTED
        self.rejection_reason = reason
        self.save(update_fields=["status", "rejection_reason", "updated_at"])

    def suspend(self, reason: str = "") -> None:
        """Suspends a user account."""
        self.status = self.AccountStatus.SUSPENDED
        self.rejection_reason = reason
        self.save(update_fields=["status", "rejection_reason", "updated_at"])

    def activate(self) -> None:
        """Activates an account after first successful login or reactivates a suspended one."""
        self.status = self.AccountStatus.ACTIVE
        self.save(update_fields=["status", "updated_at"])

    def has_custom_permission(self, permission: str) -> bool:
        """
        Checks if the user has a specific custom permission.
        """
        return permission in (self.permissions_list or [])