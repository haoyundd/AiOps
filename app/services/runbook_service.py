"""Runbook ingestion and retrieval backed by PostgreSQL/pgvector."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import or_, select

from app.config import config
from app.db import RunbookChunk, RunbookDocument, session_scope
from app.services.vector_embedding_service import get_vector_embedding_service


def split_text(content: str) -> list[str]:
    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > config.chunk_max_size:
            chunks.append(current)
            overlap = current[-config.chunk_overlap :] if config.chunk_overlap else ""
            current = f"{overlap}\n\n{paragraph}".strip()
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks or [content]


class RunbookService:
    async def create(
        self,
        *,
        title: str,
        service_name: str,
        tags: list[str],
        content: str,
        created_by: str,
    ) -> tuple[RunbookDocument, bool]:
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        async with session_scope() as session:
            existing = await session.scalar(
                select(RunbookDocument).where(RunbookDocument.checksum == checksum)
            )
            if existing is not None:
                return existing, False

            document = RunbookDocument(
                title=title,
                service_name=service_name,
                tags=tags,
                content=content,
                checksum=checksum,
                created_by=created_by,
            )
            session.add(document)
            await session.flush()

            chunks = split_text(content)
            embeddings: list[list[float] | None] = [None] * len(chunks)
            if config.dashscope_api_key:
                embeddings = list(get_vector_embedding_service().embed_documents(chunks))
            for index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
                session.add(
                    RunbookChunk(
                        document_id=document.id,
                        chunk_index=index,
                        content=chunk,
                        metadata_json={
                            "title": title,
                            "service_name": service_name,
                            "tags": tags,
                        },
                        embedding=embedding,
                    )
                )
            return document, True

    async def search(self, service_name: str, query: str, limit: int = 3) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 10))
        async with session_scope() as session:
            statement = (
                select(RunbookChunk, RunbookDocument)
                .join(RunbookDocument, RunbookDocument.id == RunbookChunk.document_id)
                .where(
                    or_(
                        RunbookDocument.service_name == service_name,
                        RunbookDocument.service_name == "*",
                    ),
                    or_(
                        RunbookChunk.content.ilike(f"%{query}%"),
                        RunbookDocument.title.ilike(f"%{query}%"),
                        RunbookDocument.service_name == service_name,
                    ),
                )
                .order_by(RunbookDocument.created_at.desc(), RunbookChunk.chunk_index)
                .limit(limit)
            )
            rows = (await session.execute(statement)).all()
            return [
                {
                    "document_id": document.id,
                    "title": document.title,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "tags": document.tags,
                }
                for chunk, document in rows
            ]


runbook_service = RunbookService()
