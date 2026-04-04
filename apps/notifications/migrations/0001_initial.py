from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('company_id', models.CharField(blank=True, default='default', max_length=64)),
                ('frontend_id', models.CharField(blank=True, db_index=True, max_length=255)),
                ('alert_type', models.CharField(choices=[
                    ('low_stock', 'Low Stock'), ('overdue', 'Overdue Payment'),
                    ('risk', 'Credit Risk'), ('sales_drop', 'Sales Drop'),
                    ('high_receivables', 'High Receivables'), ('dso', 'DSO Alert'),
                    ('concentration', 'Client Concentration'), ('churn', 'Churn'),
                    ('anomaly', 'Anomaly'), ('scheduled_report', 'Scheduled Report'),
                    ('system', 'System'),
                ], max_length=32)),
                ('severity', models.CharField(choices=[
                    ('low', 'Low'), ('medium', 'Medium'), ('critical', 'Critical'),
                ], default='medium', max_length=16)),
                ('title', models.CharField(max_length=255)),
                ('message', models.TextField()),
                ('detail', models.TextField(blank=True, default='')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('is_read', models.BooleanField(db_index=True, default=False)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['company_id', 'is_read'], name='notif_company_read_idx'),
        ),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['company_id', 'alert_type'], name='notif_company_type_idx'),
        ),
        migrations.AddConstraint(
            model_name='notification',
            constraint=models.UniqueConstraint(
                condition=models.Q(frontend_id__gt=''),
                fields=['company_id', 'frontend_id'],
                name='unique_notification_per_company',
            ),
        ),
    ]