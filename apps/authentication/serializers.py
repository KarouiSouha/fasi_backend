from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.companies.models import Company

User = get_user_model()


# =============================================================================
# READ SERIALIZERS (display)
# =============================================================================

class UserProfileSerializer(serializers.ModelSerializer):
    """
    Displays the complete profile of the logged-in user.
    Used by GET /api/users/profile/
    """
    full_name = serializers.CharField(read_only=True)
    company_name = serializers.CharField(read_only=True)
    company_industry = serializers.CharField(source="company.industry", read_only=True, default=None, allow_null=True)
    company_country = serializers.CharField(source="company.country", read_only=True, default=None, allow_null=True)
    company_city = serializers.CharField(source="company.city", read_only=True, default=None, allow_null=True)
    company_current_erp = serializers.CharField(source="company.current_erp", read_only=True, default=None, allow_null=True)
    company_phone = serializers.CharField(source="company.phone", read_only=True, default=None, allow_null=True)
    company_address = serializers.CharField(source="company.address", read_only=True, default=None, allow_null=True)
    company_is_active = serializers.BooleanField(source="company.is_active", read_only=True, default=None, allow_null=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone_number",
            "role",
            "status",
            "permissions_list",
            "company",
            "company_name",
            "company_industry",
            "company_country",
            "company_city",
            "company_current_erp",
            "company_phone",
            "company_address",
            "company_is_active",
            "must_change_password",
            "is_verified",
            "created_at",
        ]
        read_only_fields = [
            "id", "email", "role", "status", "permissions_list",
            "company", "company_name",
            "company_industry", "company_country", "company_city",
            "company_current_erp", "company_phone", "company_address", "company_is_active",
            "full_name", "must_change_password", "is_verified", "created_at",
        ]


class UserListSerializer(serializers.ModelSerializer):
    """
    Simplified display of a user in a list view.
    """
    full_name = serializers.CharField(read_only=True)
    company_name = serializers.CharField(read_only=True)
    company_industry    = serializers.CharField(source="company.industry",    read_only=True, default=None, allow_null=True)
    company_country     = serializers.CharField(source="company.country",     read_only=True, default=None, allow_null=True)
    company_city        = serializers.CharField(source="company.city",        read_only=True, default=None, allow_null=True)
    company_current_erp = serializers.CharField(source="company.current_erp", read_only=True, default=None, allow_null=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "phone_number",
            "role",
            "status",
            "company",
            "company_name",
            "company_industry",     # ← NEW
            "company_country",      # ← NEW
            "company_city",         # ← NEW
            "company_current_erp", 
            "created_at",
            "permissions_list",
        ]
        read_only_fields = fields


# =============================================================================
# WRITE SERIALIZERS (create / update)
# =============================================================================

class UpdateProfileSerializer(serializers.ModelSerializer):
    """
    Allows a user to update their own profile.
    """
    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone_number"]

    def validate_first_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("First name cannot be empty.")
        return value.strip()

    def validate_last_name(self, value):
        if not value.strip():
            raise serializers.ValidationError("Last name cannot be empty.")
        return value.strip()


class ChangePasswordSerializer(serializers.Serializer):
    """
    Allows a logged-in user to change their own password.
    """
    old_password = serializers.CharField(write_only=True, required=True)
    new_password = serializers.CharField(write_only=True, required=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True, required=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def validate(self, data):
        if data["new_password"] != data["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password_confirm": "The two passwords do not match."}
            )
        if data["old_password"] == data["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "The new password must be different from the old one."}
            )
        return data


# =============================================================================
# MANAGER SIGNUP SERIALIZER
# =============================================================================


class ManagerSignupSerializer(serializers.ModelSerializer):
    """
    Allows a manager to sign up via the public form.
    The account is created with PENDING status.

    Fields (v2):
        company_name  — creates or reuses an existing Company
        industry      — business activity / sector
        country       — country of the company
        city          — city of the company
        current_erp   — ERP / software currently used (optional)
    """
    password         = serializers.CharField(write_only=True, required=True, min_length=8)
    password_confirm = serializers.CharField(write_only=True, required=True)
    company_name     = serializers.CharField(required=True, max_length=255)
    industry         = serializers.CharField(required=True, max_length=100)
    country          = serializers.CharField(required=True, max_length=100)
    city             = serializers.CharField(required=True, max_length=100)
    current_erp      = serializers.CharField(required=False, allow_blank=True, max_length=100, default='')

    class Meta:
        model = User
        fields = [
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "company_name",
            "industry",
            "country",
            "city",
            "current_erp",
            "password",
            "password_confirm",
        ]

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("This email address is already in use.")
        return email

    def validate_company_name(self, value):
        v = value.strip()
        if not v:
            raise serializers.ValidationError("Company name is required.")
        return v

    def validate_industry(self, value):
        v = value.strip()
        if not v:
            raise serializers.ValidationError("Business activity is required.")
        return v

    def validate_country(self, value):
        v = value.strip()
        if not v:
            raise serializers.ValidationError("Country is required.")
        return v

    def validate_city(self, value):
        v = value.strip()
        if not v:
            raise serializers.ValidationError("City is required.")
        return v

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def validate(self, data):
        if data["password"] != data["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "The two passwords do not match."}
            )
        return data

    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password     = validated_data.pop("password")
        company_name = validated_data.pop("company_name")
        industry     = validated_data.pop("industry")
        country      = validated_data.pop("country")
        city         = validated_data.pop("city")
        current_erp  = validated_data.pop("current_erp", "")

        # Get or create Company, storing all new fields on creation
        company, created = Company.objects.get_or_create(
            name=company_name,
            defaults={
                "industry":   industry,
                "country":    country,
                "city":       city,
                "current_erp": current_erp,
            },
        )

        # If company already existed, patch blank fields
        if not created:
            changed = False
            for field, val in [
                ("industry",    industry),
                ("country",     country),
                ("city",        city),
                ("current_erp", current_erp),
            ]:
                if not getattr(company, field, None) and val:
                    setattr(company, field, val)
                    changed = True
            if changed:
                company.save()

        user = User(
            role=User.Role.MANAGER,
            status=User.AccountStatus.PENDING,
            is_verified=False,
            must_change_password=False,
            company=company,
            **validated_data,
        )
        user.set_password(password)
        user.save()
        return user
    
    
# =============================================================================
# AGENT CREATION BY MANAGER
# =============================================================================

class CreateAgentSerializer(serializers.ModelSerializer):
    """
    Allows a manager to create an agent account.
    The agent's Company is automatically set to the Manager's — not editable.
    """
    temporary_password = serializers.CharField(
        write_only=True,
        required=True,
        min_length=8,
        help_text="Temporary password for the agent.",
    )

    class Meta:
        model = User
        fields = [
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "permissions_list",
            "temporary_password",
        ]

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError("This email address is already in use.")
        return email

    def validate_permissions_list(self, value):
        if "view-team" in (value or []):
            raise serializers.ValidationError("'view-team' permission is not allowed for agents.")

        allowed_permissions = {
            "import-data",
            "view-dashboard",
            "view-reports",
            "view-kpi",
            "view-sales",
            "view-inventory",
            "view-aging",
            "receive-notifications",
            "view-profile",
            "ai-insights",
        }
        invalid = set(value) - allowed_permissions
        if invalid:
            raise serializers.ValidationError(
                f"Invalid permissions: {', '.join(invalid)}."
            )
        if "view-dashboard" not in (value or []):
            raise serializers.ValidationError("'view-dashboard' permission is required.")
        return value

    def create(self, validated_data):
        temporary_password = validated_data.pop("temporary_password")
        manager = self.context["request"].user

        user = User(
            role=User.Role.AGENT,
            status=User.AccountStatus.ACTIVE,
            is_verified=True,
            must_change_password=True,
            created_by=manager,
            # Agent automatically inherits the Manager's Company
            company=manager.company,
            **validated_data,
        )
        user.set_password(temporary_password)
        user.save()
        return user


# =============================================================================
# PASSWORD RESET SERIALIZERS
# =============================================================================

class RequestPasswordResetSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=True)

    def validate_user_id(self, value):
        try:
            User.objects.get(id=value)
        except User.DoesNotExist:
            raise serializers.ValidationError("User not found.")
        return value


class ConfirmPasswordResetSerializer(serializers.Serializer):
    token = serializers.CharField(required=True)
    new_password = serializers.CharField(write_only=True, required=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True, required=True)

    def validate_new_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def validate(self, data):
        if data["new_password"] != data["new_password_confirm"]:
            raise serializers.ValidationError(
                {"new_password_confirm": "The two passwords do not match."}
            )
        return data


# =============================================================================
# USER MANAGEMENT SERIALIZERS (ADMIN / MANAGER)
# =============================================================================

class UpdateUserPermissionsSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["permissions_list"]

    def validate_permissions_list(self, value):
        allowed_permissions = {
            "import-data",
            "view-dashboard",
            "view-team",
            "view-reports",
            "view-kpi",
            "view-sales",
            "view-inventory",
            "view-aging",
            "receive-notifications",
            "view-profile",
            "ai-insights",
        }
        
        # Pour les agents, exclure les permissions liées à la gestion des équipes
        if self.instance and self.instance.role == User.Role.AGENT:
            allowed_permissions.discard("view-team")
        
        invalid = set(value) - allowed_permissions
        if invalid:
            raise serializers.ValidationError(
                f"Invalid permissions: {', '.join(invalid)}."
            )
        if "view-dashboard" not in value:
            raise serializers.ValidationError(
                "'view-dashboard' permission is required."
            )
        return value


class UpdateUserStatusSerializer(serializers.ModelSerializer):
    reason = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ["status", "reason"]

    def validate_status(self, value):
        allowed = [User.AccountStatus.ACTIVE, User.AccountStatus.SUSPENDED]
        if value not in allowed:
            raise serializers.ValidationError(
                "Only 'active' and 'suspended' statuses can be modified via this endpoint."
            )
        return value


class ApproveRejectManagerSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject"], required=True)
    reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, data):
        if data["action"] == "reject" and not data.get("reason", "").strip():
            raise serializers.ValidationError(
                {"reason": "A reason is required to reject a request."}
            )
        return data