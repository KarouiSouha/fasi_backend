from rest_framework import serializers
from .models import InventorySnapshot, InventorySnapshotLine


class InventorySnapshotLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InventorySnapshotLine
        fields = [
            "id",
            "product_category",
            "product_code",
            "product_name",
            "branch_name",
            "quantity",
            "unit_cost",
            "line_value",
        ]
        read_only_fields = fields


class InventorySnapshotSerializer(serializers.ModelSerializer):
    """Full detail serializer — includes aggregated summary fields."""

    line_count = serializers.IntegerField(read_only=True, default=0)
    total_lines_value = serializers.DecimalField(
        max_digits=18, decimal_places=4, read_only=True, default=0
    )
    branches = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = InventorySnapshot
        fields = [
            "id",
            "company_name",
            "label",
            "inventory_year",
            "snapshot_date",
            "fiscal_year",
            "source_file",
            "notes",
            "uploaded_at",
            "uploaded_by",
            "uploaded_by_name",
            "line_count",
            "total_lines_value",
            "branches",
        ]
        read_only_fields = fields

    def get_uploaded_by_name(self, obj):
        if obj.uploaded_by:
            return obj.uploaded_by.get_full_name() or obj.uploaded_by.username
        return None

    def get_branches(self, obj):
        return list(
            obj.lines.values_list("branch_name", flat=True)
            .distinct()
            .order_by("branch_name")
        )


class InventorySnapshotListSerializer(serializers.ModelSerializer):
    """Lightweight list serializer — uses annotated fields from the queryset."""

    line_count = serializers.IntegerField(read_only=True, default=0)
    total_lines_value = serializers.DecimalField(
        max_digits=18, decimal_places=4, read_only=True, default=0
    )

    class Meta:
        model = InventorySnapshot
        fields = [
            "id",
            "company_name",
            "label",
            "inventory_year",
            "source_file",
            "snapshot_date",
            "fiscal_year",
            "uploaded_at",
            "line_count",
            "total_lines_value",
        ]
        read_only_fields = fields