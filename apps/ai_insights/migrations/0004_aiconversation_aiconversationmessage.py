from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("ai_insights", "0003_aiusagelog"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("companies", "0004_company_city_company_country_company_current_erp"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIConversation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, db_index=True, verbose_name="Updated at")),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_conversations",
                        to="companies.company",
                        verbose_name="Company",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_conversations",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI Conversation",
                "verbose_name_plural": "AI Conversations",
                "db_table": "ai_conversation",
                "ordering": ["-updated_at"],
            },
        ),
        migrations.CreateModel(
            name="AIConversationMessage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("role", models.CharField(choices=[("user", "User"), ("assistant", "Assistant")], max_length=20)),
                ("content", models.TextField()),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="ai_insights.aiconversation",
                        verbose_name="Conversation",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI Conversation Message",
                "verbose_name_plural": "AI Conversation Messages",
                "db_table": "ai_conversation_message",
                "ordering": ["created_at"],
            },
        ),
    ]
