from django.contrib import admin
from .models import InventorySnapshotLine


@admin.register(InventorySnapshotLine)
class InventorySnapshotLineAdmin(admin.ModelAdmin):
    list_display = [
        "company_name", "inventory_year", "product_code", "product_name",
        "branch_name", "quantity", "unit_cost", "line_value",
    ]
    list_filter = ["branch_name", "company_name", "inventory_year"]
    search_fields = ["product_code", "product_name", "branch_name", "source_file", "company_name"]
    readonly_fields = ["id"]
    ordering = ["company_name", "product_code", "branch_name"]
    list_per_page = 100
