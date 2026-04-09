from rest_framework import serializers
from django.db.models import Sum

from apps.inventory.models import InventorySnapshotLine
from .models import Product


class ProductListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views."""

    class Meta:
        model = Product
        fields = [
            "id", "product_code", "lab_code",
            "product_name", "category",
        ]
        read_only_fields = fields


class ProductDetailSerializer(serializers.ModelSerializer):
    """Full serializer with aggregated stats."""

    movement_count = serializers.SerializerMethodField()
    latest_snapshot_date = serializers.SerializerMethodField()
    total_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id", "company", "product_code", "lab_code",
            "product_name", "category",
            "movement_count", "latest_snapshot_date", "total_stock",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_movement_count(self, obj):
        return obj.movements.count()

    def get_latest_snapshot_date(self, obj):
        latest = (
            InventorySnapshotLine.objects
            .filter(company=obj.company, product_code=obj.product_code)
            .order_by("-uploaded_at")
            .values_list("uploaded_at", flat=True)
            .first()
        )
        return latest.date() if latest else None

    def get_total_stock(self, obj):
        total = (
            InventorySnapshotLine.objects
            .filter(company=obj.company, product_code=obj.product_code)
            .aggregate(v=Sum("quantity"))
            .get("v")
        )
        return float(total) if total is not None else None


class ProductWriteSerializer(serializers.ModelSerializer):
    """Write serializer for create/update operations (admin only)."""

    class Meta:
        model = Product
        fields = ["product_code", "lab_code", "product_name", "category"]

    def validate_product_code(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Product code cannot be empty.")
        company = self.context["request"].user.company
        qs = Product.objects.filter(company=company, product_code=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                f"Product code '{value}' already exists in your company."
            )
        return value
