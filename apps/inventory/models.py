import uuid
from django.conf import settings
from django.db import models

class InventorySnapshotLine(models.Model):
    """
    Current inventory row per (company × product × branch).

    Produced by melting a horizontal Excel row into vertical lines.
    branch_name is plain text — no FK to branches_branch.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    company = models.ForeignKey(
        "companies.Company",
        on_delete=models.CASCADE,
        related_name="inventory_lines",
        verbose_name="Company",
    )
    company_name = models.CharField(
        max_length=200,
        verbose_name="Company Name",
        help_text="Name of the company that owns this inventory row.",
    )
    inventory_year = models.IntegerField(
        null=True,
        blank=True,
        verbose_name="Inventory Year",
        help_text="4-digit fiscal year extracted from the uploaded filename.",
    )
    source_file = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="Source File",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="Uploaded At")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_lines_uploaded",
        verbose_name="Uploaded By",
    )

    product_category = models.CharField(
        max_length=200, blank=True, default="", verbose_name="Category",
    )
    product_code = models.CharField(max_length=100, verbose_name="Product Code")
    product_name = models.CharField(
        max_length=500, blank=True, default="", verbose_name="Product Name",
    )

    branch_name = models.CharField(
        max_length=200,
        verbose_name="Branch Name",
        help_text="Plain-text branch name extracted from the Excel header. No FK.",
    )

    quantity = models.DecimalField(
        max_digits=14, decimal_places=4, default=0, verbose_name="Quantity",
    )
    unit_cost = models.DecimalField(
        max_digits=14, decimal_places=4, default=0, verbose_name="Unit Cost",
    )
    line_value = models.DecimalField(
        max_digits=18, decimal_places=4, default=0, verbose_name="Line Value",
    )

    class Meta:
        db_table = "inventory_snapshot_line"
        verbose_name = "Inventory Line"
        verbose_name_plural = "Inventory Lines"
        ordering = ["product_code", "branch_name"]
        unique_together = [("company", "product_code", "branch_name")]

    def __str__(self):
        return f"{self.company_name} | {self.product_code} | {self.branch_name} | qty={self.quantity}"
