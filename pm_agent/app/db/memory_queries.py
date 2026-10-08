"""
Database queries for the memory_chunks table and clarifications table.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import Clarification, MemoryChunk


# ── Memory Chunks ──


async def insert_memory_chunk(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_type: str,
    text_content: str,
    embedding: list[float] | None = None,
    source_id: uuid.UUID | None = None,
    importance: int = 3,
) -> MemoryChunk:
    """Create a new memory chunk with optional embedding."""
    chunk = MemoryChunk(
        project_id=project_id,
        source_type=source_type,
        source_id=source_id,
        text=text_content,
        embedding=embedding,
        importance=importance,
    )
    db.add(chunk)
    await db.flush()
    return chunk


async def search_similar_chunks(
    db: AsyncSession,
    project_id: uuid.UUID,
    query_embedding: list[float],
    limit: int = 5,
) -> list[MemoryChunk]:
    """
    Search for similar memory chunks using pgvector cosine distance.
    Returns chunks ordered by similarity (most similar first).
    """
    # Use raw SQL for pgvector cosine distance operator
    stmt = (
        select(MemoryChunk)
        .where(
            and_(
                MemoryChunk.project_id == project_id,
                MemoryChunk.embedding.isnot(None),
            )
        )
        .order_by(MemoryChunk.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_memory_by_source(
    db: AsyncSession,
    source_type: str,
    source_id: uuid.UUID,
) -> list[MemoryChunk]:
    """Get memory chunks by source type and ID."""
    result = await db.execute(
        select(MemoryChunk).where(
            and_(
                MemoryChunk.source_type == source_type,
                MemoryChunk.source_id == source_id,
            )
        )
    )
    return list(result.scalars().all())


async def get_project_memory(
    db: AsyncSession,
    project_id: uuid.UUID,
    source_type: str | None = None,
) -> list[MemoryChunk]:
    """Get all memory chunks for a project, optionally filtered by type."""
    conditions = [MemoryChunk.project_id == project_id]
    if source_type:
        conditions.append(MemoryChunk.source_type == source_type)

    result = await db.execute(
        select(MemoryChunk)
        .where(and_(*conditions))
        .order_by(MemoryChunk.created_at.desc())
    )
    return list(result.scalars().all())


# ── Clarifications ──


async def insert_clarification(
    db: AsyncSession,
    project_id: uuid.UUID,
    question: str,
    context: str | None = None,
    assigned_to_slack_id: str | None = None,
    source_message_ids: list | None = None,
    source_refs: list | None = None,
    source_suggestion_id: uuid.UUID | None = None,
    payload: dict | None = None,
    status: str = "pending",
) -> Clarification:
    """Create a new clarification.

    payload carries the structured decision-request fields (options,
    recommended_option, why_it_matters, known_context, answer_owner, confidence,
    submitter_slack_id). status defaults to pending.
    """
    clarification = Clarification(
        project_id=project_id,
        question=question,
        context=context,
        status=status,
        assigned_to_slack_id=assigned_to_slack_id,
        source_message_ids=source_message_ids or [],
        source_refs=source_refs or [],
        source_suggestion_id=source_suggestion_id,
        payload=payload or {},
    )
    db.add(clarification)
    await db.flush()
    return clarification


async def get_clarifications(
    db: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = None,
) -> list[Clarification]:
    """Get clarifications for a project, optionally filtered by status."""
    conditions = [Clarification.project_id == project_id]
    if status:
        conditions.append(Clarification.status == status)

    result = await db.execute(
        select(Clarification)
        .where(and_(*conditions))
        .order_by(Clarification.created_at.asc())
    )
    return list(result.scalars().all())


async def get_pending_clarifications(
    db: AsyncSession, project_id: uuid.UUID
) -> list[Clarification]:
    """Get all pending clarifications for a project."""
    return await get_clarifications(db, project_id, status="pending")


async def update_clarification_status(
    db: AsyncSession,
    clarification_id: uuid.UUID,
    status: str,
    answer_message_id: uuid.UUID | None = None,
) -> Clarification | None:
    """Update a clarification's status."""
    clarification = await db.get(Clarification, clarification_id)
    if clarification is None:
        return None

    clarification.status = status
    clarification.updated_at = datetime.now(timezone.utc)
    if answer_message_id:
        clarification.answer_message_id = answer_message_id

    await db.flush()
    return clarification


async def mark_clarification_answered(
    db: AsyncSession,
    clarification_id: uuid.UUID,
    final_answer: str,
    answered_by_slack_id: str | None = None,
) -> Clarification | None:
    """Record a confirmed answer and mark a clarification as answered."""
    clarification = await db.get(Clarification, clarification_id)
    if clarification is None:
        return None

    now = datetime.now(timezone.utc)
    clarification.status = "answered"
    clarification.final_answer = final_answer
    clarification.answered_by_slack_id = answered_by_slack_id
    clarification.answered_at = now
    clarification.updated_at = now

    await db.flush()
    return clarification
