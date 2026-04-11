import logging
from django.conf import settings

from apps.ai_insights.client import AIClient

logger = logging.getLogger(__name__)


class OpenAIService:
    def __init__(self):
        self._client = AIClient()
        self.embedding_model = getattr(settings, "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

    def embed_texts(self, texts):
        """Return OpenAI embeddings for a list of texts."""
        client = self._client._get_client()
        response = client.embeddings.create(
            model=self.embedding_model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    def complete(self, system_prompt, user_prompt, analyzer="rag", max_tokens=600):
        return self._client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model="smart",
            max_tokens=max_tokens,
            analyzer=analyzer,
        )
