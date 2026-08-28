"""Lazy DashScope embedding client used by pgvector runbooks."""

from functools import lru_cache
from typing import cast

from langchain_core.embeddings import Embeddings
from openai import OpenAI

from app.config import config


class DashScopeEmbeddings(Embeddings):
    def __init__(self, api_key: str, model: str, dimensions: int) -> None:
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY is required only when creating embeddings")
        self.client = OpenAI(api_key=api_key, base_url=config.dashscope_api_base)
        self.model = model
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(
            model=self.model,
            input=texts,
            dimensions=self.dimensions,
            encoding_format="float",
        )
        return [item.embedding for item in response.data]

    def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("embedding query cannot be empty")
        response = self.client.embeddings.create(
            model=self.model,
            input=text,
            dimensions=self.dimensions,
            encoding_format="float",
        )
        return cast(list[float], response.data[0].embedding)


@lru_cache(maxsize=1)
def get_vector_embedding_service() -> DashScopeEmbeddings:
    return DashScopeEmbeddings(
        api_key=config.dashscope_api_key,
        model=config.dashscope_embedding_model,
        dimensions=config.embedding_dimensions,
    )
