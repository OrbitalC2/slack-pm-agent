"""
Database queries for the suggestions table.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import Suggestion


async def insert_suggestion(
    db: AsyncSession,
    project_id: uuid.UUID,
    suggestion_type: str,
    title: str,
    body: str,
    gather_run_id: uuid.UUID | None = None,
    source_message_ids: list | None = None,
    source_refs: list | None = None,
    payload: dict | None = None,
) -> Suggestion:
    """Create a new suggestion (status=pending)."""
    suggestion = Suggestion(
        project_id=project_id,
        gather_run_id=gather_run_id,
        suggestion_type=suggestion_type,
        title=title,
        body=body,
        source_message_ids=source_message_ids or [],
        source_refs=source_refs or [],
        payload=payload or {},
        status="pending",
    )
    db.add(suggestion)
    await db.flush()
    return suggestion


async def get_suggestion_by_id(
    db: AsyncSession, suggestion_id: uuid.UUID
) -> Suggestion | None:
    """Get a suggestion by ID."""
    return await db.get(Suggestion, suggestion_id)


async def get_pending_suggestions(
    db: AsyncSession,
    project_id: uuid.UUID,
    suggestion_type: str | None = None,
) -> list[Suggestion]:
    """Get all pending suggestions for a project, optionally filtered by type."""
    conditions = [
        Suggestion.project_id == project_id,
        Suggestion.status == "pending",
    ]
    if suggestion_type:
        conditions.append(Suggestion.suggestion_type == suggestion_type)

    result = await db.execute(
        select(Suggestion)
        .where(and_(*conditions))
        .order_by(Suggestion.created_at.asc())
    )
    return list(result.scalars().all())


async def get_pending_tasks_for_gather_run(
    db: AsyncSession, gather_run_id: uuid.UUID
) -> list[Suggestion]:
    """Get all pending task suggestions for a specific gather run."""
    result = await db.execute(
        select(Suggestion).where(
            and_(
                Suggestion.gather_run_id == gather_run_id,
                Suggestion.suggestion_type == "task",
                Suggestion.status == "pending",
            )
        )
    )
    return list(result.scalars().all())


async def update_suggestion_status(
    db: AsyncSession,
    suggestion_id: uuid.UUID,
    status: str,
    approved_by_slack_id: str | None = None,
) -> Suggestion | None:
    """Update a suggestion's status."""
    suggestion = await get_suggestion_by_id(db, suggestion_id)
    if suggestion is None:
        return None

    suggestion.status = status
    suggestion.updated_at = datetime.now(timezone.utc)
    if approved_by_slack_id:
        suggestion.approved_by_slack_id = approved_by_slack_id

    await db.flush()
    return suggestion


async def claim_pending_suggestion(
    db: AsyncSession,
    suggestion_id: uuid.UUID,
    approved_by_slack_id: str,
) -> Suggestion | None:
    """
    Atomically claim a pending suggestion for approval.

    Returns the suggestion only if this call changed pending -> approved.
    Concurrent retries/double-clicks return None after the first claim wins.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        update(Suggestion)
        .where(
            and_(
                Suggestion.id == suggestion_id,
                Suggestion.status == "pending",
            )
        )
        .values(
            status="approved",
            approved_by_slack_id=approved_by_slack_id,
            updated_at=now,
        )
        .returning(Suggestion.id)
    )
    result = await db.execute(stmt)
    claimed_id = result.scalar_one_or_none()
    if claimed_id is None:
        return None

    await db.flush()
    return await db.get(Suggestion, claimed_id)


async def update_suggestion_content(
    db: AsyncSession,
    suggestion_id: uuid.UUID,
    title: str | None = None,
    body: str | None = None,
    payload: dict | None = None,
) -> Suggestion | None:
    """Update a suggestion's content (for edit flow)."""
    suggestion = await get_suggestion_by_id(db, suggestion_id)
    if suggestion is None:
        return None

    if title is not None:
        suggestion.title = title
    if body is not None:
        suggestion.body = body
    if payload is not None:
        suggestion.payload = payload
    suggestion.updated_at = datetime.now(timezone.utc)

    await db.flush()
    return suggestion


async def get_approved_suggestions(
    db: AsyncSession,
    project_id: uuid.UUID,
    suggestion_type: str | None = None,
) -> list[Suggestion]:
    """Get approved suggestions for a project, optionally by type."""
    conditions = [
        Suggestion.project_id == project_id,
        Suggestion.status == "approved",
    ]
    if suggestion_type:
        conditions.append(Suggestion.suggestion_type == suggestion_type)

    result = await db.execute(
        select(Suggestion)
        .where(and_(*conditions))
        .order_by(Suggestion.created_at.asc())
    )
    return list(result.scalars().all())
