"""Database queries for Slack documents and parsed document chunks."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import Document, DocumentChunk


DOCUMENT_METADATA_FIELDS = (
    "slack_user_id",
    "filename",
    "title",
    "mimetype",
    "filetype",
    "pretty_type",
    "size_bytes",
    "url_private",
    "url_private_download",
    "permalink",
)


async def upsert_document_metadata(
    db: AsyncSession,
    project_id: uuid.UUID,
    slack_file_id: str,
    slack_channel_id: str,
    source_message_id: uuid.UUID | None = None,
    **metadata,
) -> tuple[Document, bool]:
    """Insert file provenance or enrich the existing Slack file row."""
    values = {
        "project_id": project_id,
        "source_message_id": source_message_id,
        "slack_file_id": slack_file_id,
        "slack_channel_id": slack_channel_id,
    }
    values.update({key: metadata.get(key) for key in DOCUMENT_METADATA_FIELDS})

    insert_stmt = pg_insert(Document).values(**values)
    excluded = insert_stmt.excluded
    update_values = {
        "slack_channel_id": excluded.slack_channel_id,
        "source_message_id": func.coalesce(Document.source_message_id, excluded.source_message_id),
        "updated_at": datetime.now(timezone.utc),
    }
    for field in DOCUMENT_METADATA_FIELDS:
        update_values[field] = func.coalesce(getattr(excluded, field), getattr(Document, field))

    stmt = (
        insert_stmt.on_conflict_do_update(
            index_elements=["slack_file_id"],
            set_=update_values,
        )
        .returning(Document.id, literal_column("xmax = 0").label("inserted"))
    )
    result = await db.execute(stmt)
    document_id, inserted = result.one()
    await db.flush()
    return await db.get(Document, document_id), bool(inserted)


async def get_document(db: AsyncSession, document_id: uuid.UUID) -> Document | None:
    return await db.get(Document, document_id)


async def get_document_by_slack_file_id(
    db: AsyncSession, slack_file_id: str
) -> Document | None:
    result = await db.execute(
        select(Document).where(Document.slack_file_id == slack_file_id)
    )
    return result.scalar_one_or_none()


async def update_document(db: AsyncSession, document: Document, **values) -> Document:
    """Update known document fields and refresh updated_at."""
    for key, value in values.items():
        if hasattr(document, key):
            setattr(document, key, value)
    document.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return document


async def replace_document_chunks(
    db: AsyncSession,
    document: Document,
    chunks: list[dict],
) -> list[DocumentChunk]:
    """Replace all parsed chunks for a document deterministically."""
    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    rows = [
        DocumentChunk(
            document_id=document.id,
            project_id=document.project_id,
            chunk_index=chunk["chunk_index"],
            page_number=chunk.get("page_number"),
            text=chunk["text"],
            embedding=None,
        )
        for chunk in chunks
    ]
    db.add_all(rows)
    await db.flush()
    return rows


async def get_document_chunks(
    db: AsyncSession, document_id: uuid.UUID
) -> list[DocumentChunk]:
    result = await db.execute(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index.asc())
    )
    return list(result.scalars().all())


async def update_chunk_embedding(
    db: AsyncSession,
    chunk_id: uuid.UUID,
    embedding: list[float],
) -> None:
    chunk = await db.get(DocumentChunk, chunk_id)
    if chunk is not None:
        chunk.embedding = embedding
        await db.flush()


async def search_similar_document_chunks(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_embedding: list[float],
    limit: int = 6,
) -> list[tuple[DocumentChunk, Document]]:
    """Return the most similar embedded document chunks with their parent document.

    Chunks whose embedding is NULL are skipped, so an un-embedded (or skipped)
    document never participates in vector retrieval.
    """
    stmt = (
        select(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.project_id == project_id,
            DocumentChunk.embedding.isnot(None),
        )
        .order_by(DocumentChunk.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def get_documents_for_messages(
    db: AsyncSession,
    project_id: uuid.UUID,
    message_ids: list[uuid.UUID],
) -> list[tuple[Document, int]]:
    """Return linked documents and chunk counts for gather context."""
    if not message_ids:
        return []
    result = await db.execute(
        select(Document, func.count(DocumentChunk.id))
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(
            Document.project_id == project_id,
            Document.source_message_id.in_(message_ids),
        )
        .group_by(Document.id)
        .order_by(Document.created_at.asc())
    )
    return [(row[0], int(row[1])) for row in result.all()]


async def get_unconsumed_completed_documents(
    db: AsyncSession,
    project_id: uuid.UUID,
    channel_id: str,
    limit: int = 25,
) -> list[tuple[Document, int]]:
    """Return completed-analysis documents for a channel not yet consumed by gather.

    These are documents whose parsing/analysis finished after their upload
    message was already gathered. They must still be included in a future gather
    run even though the original upload message is behind the gather pointer.
    """
    result = await db.execute(
        select(Document, func.count(DocumentChunk.id))
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(
            Document.project_id == project_id,
            Document.slack_channel_id == channel_id,
            Document.analysis_status == "completed",
            Document.analysis_consumed_at.is_(None),
        )
        .group_by(Document.id)
        .order_by(Document.created_at.asc())
        .limit(limit)
    )
    return [(row[0], int(row[1])) for row in result.all()]


async def mark_documents_consumed(
    db: AsyncSession,
    document_ids: list[uuid.UUID],
    gather_run_id: uuid.UUID,
) -> None:
    """Mark completed-analysis documents as consumed by a specific gather run."""
    ids = [document_id for document_id in document_ids if document_id is not None]
    if not ids:
        return
    await db.execute(
        update(Document)
        .where(Document.id.in_(ids))
        .values(
            analysis_consumed_at=datetime.now(timezone.utc),
            last_included_in_gather_run_id=gather_run_id,
        )
    )
    await db.flush()


async def get_recent_documents_with_chunk_counts(
    db: AsyncSession,
    project_id: uuid.UUID,
    limit: int = 11,
) -> list[tuple[Document, int]]:
    """Return recent project documents with persisted chunk counts."""
    result = await db.execute(
        select(Document, func.count(DocumentChunk.id))
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .where(Document.project_id == project_id)
        .group_by(Document.id)
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    return [(row[0], int(row[1])) for row in result.all()]
