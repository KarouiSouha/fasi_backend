import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def copy_snapshot_metadata_to_lines(apps, schema_editor):
    InventorySnapshot = apps.get_model("inventory", "InventorySnapshot")
    InventorySnapshotLine = apps.get_model("inventory", "InventorySnapshotLine")

    for line in InventorySnapshotLine.objects.select_related("snapshot").all().iterator():
        snap = line.snapshot
        if not snap:
            continue
        line.company_id = snap.company_id
        line.company_name = snap.company_name or ""
        line.inventory_year = snap.inventory_year
        line.source_file = snap.source_file or ""
        line.uploaded_at = snap.uploaded_at
        line.uploaded_by_id = snap.uploaded_by_id
        line.save(
            update_fields=[
                "company",
                "company_name",
                "inventory_year",
                "source_file",
                "uploaded_at",
                "uploaded_by",
            ]
        )


def deduplicate_company_product_branch(apps, schema_editor):
    InventorySnapshotLine = apps.get_model("inventory", "InventorySnapshotLine")

    seen = set()
    duplicate_ids = []
    rows = (
        InventorySnapshotLine.objects
        .order_by("company_id", "product_code", "branch_name", "-uploaded_at", "-id")
        .values("id", "company_id", "product_code", "branch_name")
    )

    for row in rows.iterator():
        key = (row["company_id"], row["product_code"], row["branch_name"])
        if key in seen:
            duplicate_ids.append(row["id"])
        else:
            seen.add(key)

    if duplicate_ids:
        InventorySnapshotLine.objects.filter(id__in=duplicate_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("companies", "0003_alter_company_options_alter_company_address_and_more"),
        ("inventory", "0008_alter_inventorysnapshot_label"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorysnapshotline",
            name="company",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="inventory_lines",
                to="companies.company",
                verbose_name="Company",
            ),
        ),
        migrations.AddField(
            model_name="inventorysnapshotline",
            name="company_name",
            field=models.CharField(
                max_length=200,
                default="",
                verbose_name="Company Name",
                help_text="Name of the company that owns this inventory row.",
            ),
        ),
        migrations.AddField(
            model_name="inventorysnapshotline",
            name="inventory_year",
            field=models.IntegerField(
                null=True,
                blank=True,
                verbose_name="Inventory Year",
                help_text="4-digit fiscal year extracted from the uploaded filename.",
            ),
        ),
        migrations.AddField(
            model_name="inventorysnapshotline",
            name="source_file",
            field=models.CharField(
                max_length=500,
                blank=True,
                default="",
                verbose_name="Source File",
            ),
        ),
        migrations.AddField(
            model_name="inventorysnapshotline",
            name="uploaded_at",
            field=models.DateTimeField(auto_now_add=True, null=True, verbose_name="Uploaded At"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="inventorysnapshotline",
            name="uploaded_by",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="inventory_lines_uploaded",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Uploaded By",
            ),
        ),
        migrations.RunPython(copy_snapshot_metadata_to_lines, migrations.RunPython.noop),
        migrations.RunPython(deduplicate_company_product_branch, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="inventorysnapshotline",
            name="company",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="inventory_lines",
                to="companies.company",
                verbose_name="Company",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="inventorysnapshotline",
            unique_together={("company", "product_code", "branch_name")},
        ),
        migrations.RemoveField(
            model_name="inventorysnapshotline",
            name="snapshot",
        ),
        migrations.DeleteModel(
            name="InventorySnapshot",
        ),
    ]
