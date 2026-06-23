# Generated migration to add email_verified to User
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ("authentication", "0006_alter_user_company"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_verified",
            field=models.BooleanField(default=False, verbose_name='Email ownership verified', help_text=('True when the user has clicked the verification link sent to their email. This is separate from `is_verified` which is reserved for admin approval.')),
        ),
    ]
