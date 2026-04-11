import logging
from django.conf import settings

logger = logging.getLogger(__name__)


class QdrantServiceUnavailable(Exception):
    pass


class QdrantService:
    def __init__(self):
        self.url = getattr(settings, "QDRANT_URL", "").strip()
        self.api_key = getattr(settings, "QDRANT_API_KEY", "").strip()
        self.collection_name = getattr(settings, "QDRANT_COLLECTION_NAME", "weeg_documents")
        self.distance = getattr(settings, "QDRANT_DISTANCE", "cosine")

        if not self.url:
            raise QdrantServiceUnavailable("QDRANT_URL is not configured.")

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models as rest
        except ImportError as exc:
            raise QdrantServiceUnavailable("qdrant-client is not installed.") from exc

        self._client_class = QdrantClient
        self._rest = rest
        self.client = QdrantClient(url=self.url, api_key=self.api_key)
        self._ensure_collection()

    def _ensure_collection(self):
        try:
            self.client.get_collection(collection_name=self.collection_name)
        except Exception:
            try:
                self.client.recreate_collection(
                    collection_name=self.collection_name,
                    vectors_config=self._rest.VectorParams(
                        size=getattr(settings, "OPENAI_EMBEDDING_DIM", 1536),
                        distance=self._rest.Distance[self.distance.upper()],
                    ),
                )
            except Exception as exc:
                logger.warning("[QdrantService] Unable to create collection: %s", exc)
                raise QdrantServiceUnavailable("Unable to initialize Qdrant collection.") from exc

    def upsert_documents(self, records):
        return self.client.upsert(
            collection_name=self.collection_name,
            points=records,
        )

    def search(self, query_embedding, company_id=None, top=5):
        query_filter = None
        if company_id:
            query_filter = self._rest.Filter(
                must=[
                    self._rest.FieldCondition(
                        key="company_id",
                        match=self._rest.MatchValue(value=company_id),
                    )
                ]
            )

        return self.client.search(
            collection_name=self.collection_name,
            query_vector=query_embedding,
            limit=top,
            with_payload=True,
            query_filter=query_filter,
        )

    def delete_all_documents(self):
        return self.client.delete(
            collection_name=self.collection_name,
            points_selector=self._rest.PointIdsList([]),
        )
