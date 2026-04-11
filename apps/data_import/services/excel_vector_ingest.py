import logging
from datetime import date

from django.conf import settings

logger = logging.getLogger(__name__)


class ExcelVectorIngestService:
    def __init__(self):
        from apps.ai_insights.services.qdrant_service import QdrantService, QdrantServiceUnavailable
        from apps.ai_insights.services.openai_service import OpenAIService

        self.openai = OpenAIService()
        try:
            self.qdrant = QdrantService()
        except QdrantServiceUnavailable as exc:
            self.qdrant = None
            logger.warning("[ExcelVectorIngestService] Qdrant unavailable: %s", exc)

    def build_row_text(self, movement):
        return (
            f"Date: {movement.movement_date.isoformat()} | "
            f"Product: {movement.material_name or movement.material_code} | "
            f"Qty out: {movement.qty_out or 0} | "
            f"Total out: {movement.total_out or 0} LYD | "
            f"Type: {movement.movement_type or 'N/A'} | "
            f"Branch: {movement.branch.name if movement.branch else 'Unknown'} | "
            f"Customer: {movement.customer_name or 'Unknown'}"
        )

    def index_movements(self, company, date_range=None):
        if not self.qdrant:
            return

        from apps.transactions.models import MaterialMovement
        query = MaterialMovement.objects.filter(company=company, movement_type__icontains="بيع")
        if date_range and len(date_range) == 2 and date_range[0] and date_range[1]:
            query = query.filter(
                movement_date__gte=date_range[0],
                movement_date__lte=date_range[1],
            )
        else:
            today = date.today()
            past_year = date(today.year - 1, today.month, today.day)
            query = query.filter(movement_date__gte=past_year)

        rows = query.order_by("movement_date")[:2000]
        if not rows:
            return

        texts = [self.build_row_text(m) for m in rows]
        try:
            vectors = self.openai.embed_texts(texts)
        except Exception as exc:
            logger.warning("[ExcelVectorIngestService] Embedding failed: %s", exc)
            return

        points = []
        for movement, vector, text in zip(rows, vectors, texts):
            points.append({
                "id": str(movement.id),
                "vector": vector,
                "payload": {
                    "company_id": str(company.id),
                    "movement_date": movement.movement_date.isoformat(),
                    "material_name": movement.material_name,
                    "material_code": movement.material_code,
                    "movement_type": movement.movement_type,
                    "text": text,
                },
            })

        try:
            self.qdrant.upsert_documents(points)
            logger.info("[ExcelVectorIngestService] Indexed %d movement rows into Qdrant.", len(points))
        except Exception as exc:
            logger.warning("[ExcelVectorIngestService] Qdrant upsert failed: %s", exc)
