# Generated migration to remove email_verified from User
from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("authentication", "0007_add_email_verified"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="email_verified",
        ),
    ]
