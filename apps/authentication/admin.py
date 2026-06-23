from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """
    Custom admin interface for the custom User model.
    Note: Branch management is handled only through the business interface,
    not via the Django admin (per WEEG specifications).
    """

    list_display = [
        "email",
        "full_name",
        "role_display",
        "status_display",
        "company",
        "is_verified",
        "created_at",
    ]

    list_filter = [
        "role",
        "status",
        "is_verified",
        "must_change_password",
        "company",
        "created_at",
    ]

    search_fields = ["email", "first_name", "last_name", "phone_number"]
    ordering = ["-created_at"]
    readonly_fields = ["id", "created_at", "updated_at", "token_version", "created_by"]

    fieldsets = (
        (
            "Identity",
            {"fields": ("id", "email", "first_name", "last_name", "phone_number")},
        ),
        (
            "Role & Access",
            {"fields": ("role", "status", "company", "is_verified", "is_email_verified", "rejection_reason")},
        ),
        (
            "Granular Permissions",
            {"fields": ("permissions_list",), "classes": ("collapse",)},
        ),
        (
            "Security",
            {"fields": ("password", "token_version", "must_change_password"), "classes": ("collapse",)},
        ),
        (
            "Django System Permissions",
            {
                "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
                "classes": ("collapse",),
            },
        ),
        (
            "Metadata",
            {"fields": ("created_by", "created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "first_name",
                    "last_name",
                    "role",
                    "status",
                    "company",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    USERNAME_FIELD = "email"

    def full_name(self, obj):
        return obj.full_name
    full_name.short_description = "Full name"

    def role_display(self, obj):
        colors = {
            "admin": "#c0392b",
            "manager": "#2980b9",
            "agent": "#27ae60",
        }
        color = colors.get(obj.role, "#7f8c8d")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_role_display(),
        )
    role_display.short_description = "Role"

    def status_display(self, obj):
        colors = {
            "pending": "#f39c12",
            "approved": "#27ae60",
            "rejected": "#c0392b",
            "active": "#2ecc71",
            "suspended": "#7f8c8d",
        }
        color = colors.get(obj.status, "#7f8c8d")
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.get_status_display(),
        )
    status_display.short_description = "Status"