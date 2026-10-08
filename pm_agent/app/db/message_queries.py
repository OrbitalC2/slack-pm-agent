"""
Database queries for the messages and processed_events tables.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, and_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from pm_agent.app.db.models import Message, ProcessedEvent


# ── Processed Events (deduplication) ──


async def claim_event_processing(
    db: AsyncSession, event_id: str, event_type: str | None = None
) -> bool:
    """
    Atomically claim a Slack event ID for processing.

    Returns True when this caller moved the event into processing. Existing
    processing/completed events return False. Failed events may be reclaimed
    by Slack retries.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        pg_insert(ProcessedEvent)
        .values(
            slack_event_id=event_id,
            event_type=event_type,
            status="processing",
            received_at=now,
        )
        .on_conflict_do_update(
            index_elements=["slack_event_id"],
            set_={
                "event_type": event_type,
                "status": "processing",
                "received_at": now,
            },
            where=(ProcessedEvent.status == "failed"),
        )
        .returning(ProcessedEvent.id)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one_or_none() is not None


async def mark_event_completed(db: AsyncSession, event_id: str) -> None:
    """Mark a claimed Slack event as completed."""
    await db.execute(
        update(ProcessedEvent)
        .where(ProcessedEvent.slack_event_id == event_id)
        .values(status="completed")
    )
    await db.flush()


async def mark_event_failed(db: AsyncSession, event_id: str) -> None:
    """Mark a claimed Slack event as failed so a retry can reclaim it."""
    await db.execute(
        update(ProcessedEvent)
        .where(ProcessedEvent.slack_event_id == event_id)
        .values(status="failed")
    )
    await db.flush()


# ── Messages ──


async def insert_message(
    db: AsyncSession,
    project_id: uuid.UUID,
    slack_channel_id: str,
    slack_message_ts: str,
    raw_text: str,
    sender_slack_id: str | None = None,
    sender_type: str = "unknown",
    is_bot: bool = False,
    slack_thread_ts: str | None = None,
    message_subtype: str | None = None,
    normalized_text: str | None = None,
    permalink: str | None = None,
) -> Message | None:
    """
    Insert a message, ignoring conflicts on (channel_id, message_ts).
    Returns the Message if inserted, None if it already exists.
    """
    stmt = pg_insert(Message).values(
        project_id=project_id,
        slack_channel_id=slack_channel_id,
        slack_message_ts=slack_message_ts,
        slack_thread_ts=slack_thread_ts,
        sender_slack_id=sender_slack_id,
        sender_type=sender_type,
        is_bot=is_bot,
        message_subtype=message_subtype,
        raw_text=raw_text,
        normalized_text=normalized_text,
        permalink=permalink,
        is_deleted=False,
    ).on_conflict_do_nothing(
        constraint="uq_channel_message_ts"
    ).returning(Message.id)

    result = await db.execute(stmt)
    await db.flush()

    row = result.scalar_one_or_none()
    if row is not None:
        return await db.get(Message, row)
    return None


async def get_messages_since(
    db: AsyncSession,
    project_id: uuid.UUID,
    channel_id: str,
    since_ts: str | None = None,
) -> list[Message]:
    """
    Get messages for a project channel since a given Slack timestamp.
    If since_ts is None, returns all messages for the channel.
    Ordered by slack_message_ts ascending.
    """
    conditions = [
        Message.project_id == project_id,
        Message.slack_channel_id == channel_id,
        Message.is_deleted.is_(False),
    ]
    if since_ts is not None:
        conditions.append(Message.slack_message_ts > since_ts)

    result = await db.execute(
        select(Message)
        .where(and_(*conditions))
        .order_by(Message.slack_message_ts.asc())
    )
    return list(result.scalars().all())


async def get_message_by_id(
    db: AsyncSession, message_id: uuid.UUID
) -> Message | None:
    """Get a single message by ID."""
    return await db.get(Message, message_id)


async def get_message_by_channel_ts(
    db: AsyncSession,
    slack_channel_id: str,
    slack_message_ts: str,
) -> Message | None:
    """Get a stored Slack message by its stable channel/timestamp key."""
    result = await db.execute(
        select(Message).where(
            and_(
                Message.slack_channel_id == slack_channel_id,
                Message.slack_message_ts == slack_message_ts,
            )
        )
    )
    return result.scalar_one_or_none()


async def get_messages_by_ids(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> list[Message]:
    """Get multiple messages by their IDs."""
    if not message_ids:
        return []
    result = await db.execute(
        select(Message).where(Message.id.in_(message_ids))
    )
    return list(result.scalars().all())


async def update_message_content(
    db: AsyncSession,
    slack_channel_id: str,
    slack_message_ts: str,
    raw_text: str,
    normalized_text: str | None = None,
    sender_slack_id: str | None = None,
    sender_type: str | None = None,
    is_bot: bool | None = None,
    slack_thread_ts: str | None = None,
    message_subtype: str | None = "message_changed",
) -> Message | None:
    """Update an existing Slack message after an edit."""
    values = {
        "raw_text": raw_text,
        "normalized_text": normalized_text,
        "message_subtype": message_subtype,
        "is_deleted": False,
        "updated_at": datetime.now(timezone.utc),
    }
    if sender_slack_id is not None:
        values["sender_slack_id"] = sender_slack_id
    if sender_type is not None:
        values["sender_type"] = sender_type
    if is_bot is not None:
        values["is_bot"] = is_bot
    if slack_thread_ts is not None:
        values["slack_thread_ts"] = slack_thread_ts

    stmt = (
        update(Message)
        .where(
            and_(
                Message.slack_channel_id == slack_channel_id,
                Message.slack_message_ts == slack_message_ts,
            )
        )
        .values(**values)
        .returning(Message.id)
    )
    result = await db.execute(stmt)
    message_id = result.scalar_one_or_none()
    await db.flush()
    if message_id is None:
        return None
    return await db.get(Message, message_id)


async def mark_message_deleted(
    db: AsyncSession,
    slack_channel_id: str,
    slack_message_ts: str,
) -> Message | None:
    """Mark a stored Slack message as deleted and exclude it from future gathers."""
    stmt = (
        update(Message)
        .where(
            and_(
                Message.slack_channel_id == slack_channel_id,
                Message.slack_message_ts == slack_message_ts,
            )
        )
        .values(
            is_deleted=True,
            message_subtype="message_deleted",
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Message.id)
    )
    result = await db.execute(stmt)
    message_id = result.scalar_one_or_none()
    await db.flush()
    if message_id is None:
        return None
    return await db.get(Message, message_id)


async def update_message_permalink(
    db: AsyncSession,
    message_id: uuid.UUID,
    permalink: str,
) -> Message | None:
    """Persist a Slack permalink for a message."""
    message = await db.get(Message, message_id)
    if message is None:
        return None
    message.permalink = permalink
    message.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return message


async def bulk_insert_messages(
    db: AsyncSession,
    messages: list[dict],
) -> int:
    """
    Bulk insert messages, skipping duplicates.
    Returns count of actually inserted messages.
    """
    if not messages:
        return 0

    inserted = 0
    for msg_data in messages:
        stmt = pg_insert(Message).values(**msg_data).on_conflict_do_nothing(
            constraint="uq_channel_message_ts"
        ).returning(Message.id)
        result = await db.execute(stmt)
        if result.scalar_one_or_none() is not None:
            inserted += 1

    await db.flush()
    return inserted
