from rest_framework import serializers
from .models import InventorySnapshotLine


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


class InventorySnapshotListSerializer(serializers.Serializer):
    """Compatibility serializer that returns a synthetic single 'current stock' item."""

    id = serializers.UUIDField(read_only=True)
    company_name = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    inventory_year = serializers.IntegerField(read_only=True, allow_null=True)
    source_file = serializers.CharField(read_only=True, allow_blank=True)
    snapshot_date = serializers.DateField(read_only=True, allow_null=True)
    fiscal_year = serializers.CharField(read_only=True, allow_blank=True)
    uploaded_at = serializers.DateTimeField(read_only=True)
    line_count = serializers.IntegerField(read_only=True, default=0)
    total_lines_value = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True, default=0)


class InventorySnapshotSerializer(InventorySnapshotListSerializer):
    """Detail compatibility serializer with branch list and uploader label."""

    uploaded_by = serializers.UUIDField(read_only=True, allow_null=True)
    uploaded_by_name = serializers.CharField(read_only=True, allow_null=True)
    notes = serializers.CharField(read_only=True, allow_blank=True)
    branches = serializers.ListField(child=serializers.CharField(), read_only=True)