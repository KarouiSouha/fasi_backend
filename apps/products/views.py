from django.db.models import Q, Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Product
from .serializers import (
    ProductListSerializer,
    ProductDetailSerializer,
    ProductWriteSerializer,
)


class ProductListView(APIView):
    """
    GET  /api/products/
        Query params:
            search=<str>         — name or product_code
            category=<str>       — exact match
            ordering=<field>     — product_name | product_code | category (prefix - for DESC)
            page=<int>
            page_size=<int>      — default 50, max 200

    POST /api/products/          — admin / manager only
    """

    permission_classes = [IsAuthenticated]
    ALLOWED_ORDERINGS = {
        "product_name", "-product_name",
        "product_code", "-product_code",
        "category", "-category",
        "created_at", "-created_at",
    }

    def get(self, request):
        qs = Product.objects.filter(company=request.user.company)

        search = request.query_params.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(product_name__icontains=search) |
                Q(product_code__icontains=search) |
                Q(lab_code__icontains=search)
            )

        category = request.query_params.get("category", "").strip()
        if category:
            qs = qs.filter(category__icontains=category)

        ordering = request.query_params.get("ordering", "category")
        if ordering in self.ALLOWED_ORDERINGS:
            qs = qs.order_by(ordering)

        total_count = qs.count()
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(200, max(1, int(request.query_params.get("page_size", 50))))
        start = (page - 1) * page_size
        qs_page = qs[start: start + page_size]

        return Response({
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total_count + page_size - 1) // page_size),
            "products": ProductListSerializer(qs_page, many=True).data,
        })

    def post(self, request):
        if not (request.user.is_admin or request.user.is_manager):
            return Response(
                {"error": "Only managers and administrators can create products."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ProductWriteSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        product = serializer.save(company=request.user.company)
        return Response(
            ProductDetailSerializer(product).data,
            status=status.HTTP_201_CREATED,
        )


class ProductDetailView(APIView):
    """
    GET    /api/products/{id}/
    PATCH  /api/products/{id}/   — manager / admin only
    DELETE /api/products/{id}/   — admin only
    """

    permission_classes = [IsAuthenticated]

    def _get_product(self, product_id, company):
        try:
            return Product.objects.get(id=product_id, company=company)
        except Product.DoesNotExist:
            return None

    def get(self, request, product_id):
        product = self._get_product(product_id, request.user.company)
        if not product:
            return Response({"error": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProductDetailSerializer(product).data)

    def patch(self, request, product_id):
        if not (request.user.is_admin or request.user.is_manager):
            return Response({"error": "Insufficient permissions."}, status=status.HTTP_403_FORBIDDEN)
        product = self._get_product(product_id, request.user.company)
        if not product:
            return Response({"error": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = ProductWriteSerializer(
            product, data=request.data, partial=True, context={"request": request}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(ProductDetailSerializer(product).data)

    def delete(self, request, product_id):
        if not request.user.is_admin:
            return Response(
                {"error": "Only administrators can delete products."},
                status=status.HTTP_403_FORBIDDEN,
            )
        product = self._get_product(product_id, request.user.company)
        if not product:
            return Response({"error": "Product not found."}, status=status.HTTP_404_NOT_FOUND)
        name = product.product_name
        product.delete()
        return Response({"message": f"Product '{name}' deleted."}, status=status.HTTP_200_OK)


class ProductCategoryListView(APIView):
    """
    GET /api/products/categories/
    Returns the distinct list of product categories for the company.
    Used to populate filter dropdowns in the frontend.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        categories = (
            Product.objects.filter(company=request.user.company)
            .exclude(category__isnull=True)
            .exclude(category="")
            .values_list("category", flat=True)
            .distinct()
            .order_by("category")
        )
        return Response({"categories": list(categories)})


class ProductInventoryHistoryView(APIView):
    """
    GET /api/products/{id}/inventory/
    Returns current inventory totals for the given product across branches.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, product_id):
        try:
            product = Product.objects.get(id=product_id, company=request.user.company)
        except Product.DoesNotExist:
            return Response({"error": "Product not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.inventory.models import InventorySnapshotLine

        lines_qs = InventorySnapshotLine.objects.filter(
            company=request.user.company,
            product_code=product.product_code,
        )

        by_branch = (
            lines_qs.values("branch_name")
            .annotate(total_qty=Sum("quantity"), total_value=Sum("line_value"))
            .order_by("branch_name")
        )

        return Response({
            "product": {
                "id": str(product.id),
                "code": product.product_code,
                "name": product.product_name,
            },
            "total_qty": float(lines_qs.aggregate(v=Sum("quantity"))["v"] or 0),
            "total_value": float(lines_qs.aggregate(v=Sum("line_value"))["v"] or 0),
            "branches": [
                {
                    "branch": row["branch_name"],
                    "qty": float(row["total_qty"] or 0),
                    "value": float(row["total_value"] or 0),
                }
                for row in by_branch
            ],
        })


class ProductMovementsView(APIView):
    """
    GET /api/products/{id}/movements/
    Returns movement history for a given product.

    Query params:
        movement_type=<sale|purchase|...>
        date_from=YYYY-MM-DD
        date_to=YYYY-MM-DD
        page=<int>  page_size=<int>
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, product_id):
        try:
            product = Product.objects.get(id=product_id, company=request.user.company)
        except Product.DoesNotExist:
            return Response({"error": "Product not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.transactions.models import MaterialMovement
        from apps.transactions.serializers import MovementListSerializer

        qs = MaterialMovement.objects.filter(
            company=request.user.company,
            product=product,
        ).order_by("-movement_date")

        movement_type = request.query_params.get("movement_type")
        if movement_type:
            qs = qs.filter(movement_type=movement_type)

        date_from = request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(movement_date__gte=date_from)

        date_to = request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(movement_date__lte=date_to)

        totals = qs.aggregate(
            total_in_qty=Sum("qty_in"),
            total_out_qty=Sum("qty_out"),
            total_in_value=Sum("total_in"),
            total_out_value=Sum("total_out"),
        )

        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(100, int(request.query_params.get("page_size", 50)))
        total_count = qs.count()
        qs_page = qs[(page - 1) * page_size: page * page_size]

        return Response({
            "product": {
                "id": str(product.id),
                "code": product.product_code,
                "name": product.product_name,
            },
            "count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total_count + page_size - 1) // page_size),
            "totals": {
                "total_in_qty": float(totals["total_in_qty"] or 0),
                "total_out_qty": float(totals["total_out_qty"] or 0),
                "total_in_value": float(totals["total_in_value"] or 0),
                "total_out_value": float(totals["total_out_value"] or 0),
            },
            "movements": MovementListSerializer(qs_page, many=True).data,
        })
