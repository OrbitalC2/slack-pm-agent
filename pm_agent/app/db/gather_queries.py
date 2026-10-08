"""
Database queries for gather_runs.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import GatherRun


async def get_latest_completed_gather_for_channel(
    db: AsyncSession,
    project_id: uuid.UUID,
    slack_channel_id: str,
) -> GatherRun | None:
    """Return the latest completed gather with a usable end timestamp."""
    result = await db.execute(
        select(GatherRun)
        .where(
            and_(
                GatherRun.project_id == project_id,
                GatherRun.slack_channel_id == slack_channel_id,
                GatherRun.status == "completed",
                GatherRun.to_slack_ts.isnot(None),
            )
        )
        .order_by(GatherRun.completed_at.desc(), GatherRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_running_gather_for_channel(
    db: AsyncSession,
    project_id: uuid.UUID,
    slack_channel_id: str,
) -> GatherRun | None:
    """Return the currently running gather for a project channel, if any."""
    result = await db.execute(
        select(GatherRun)
        .where(
            and_(
                GatherRun.project_id == project_id,
                GatherRun.slack_channel_id == slack_channel_id,
                GatherRun.status == "running",
            )
        )
        .order_by(GatherRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_gather_notification_status(
    db: AsyncSession,
    gather_run_id: uuid.UUID,
    notification_status: str,
) -> GatherRun | None:
    """Update notification delivery status for a gather run."""
    gather_run = await db.get(GatherRun, gather_run_id)
    if gather_run is None:
        return None

    gather_run.notification_status = notification_status
    await db.flush()
    return gather_run


async def mark_gather_completed(
    db: AsyncSession,
    gather_run_id: uuid.UUID,
    to_slack_ts: str | None,
    notification_status: str = "pending",
) -> GatherRun | None:
    """Mark a gather run completed with its processed channel pointer."""
    gather_run = await db.get(GatherRun, gather_run_id)
    if gather_run is None:
        return None

    gather_run.status = "completed"
    gather_run.completed_at = datetime.now(timezone.utc)
    gather_run.to_slack_ts = to_slack_ts
    gather_run.notification_status = notification_status
    await db.flush()
    return gather_run


async def mark_gather_failed(
    db: AsyncSession,
    gather_run_id: uuid.UUID,
    error_message: str,
) -> GatherRun | None:
    """Mark a gather run failed."""
    gather_run = await db.get(GatherRun, gather_run_id)
    if gather_run is None:
        return None

    gather_run.status = "failed"
    gather_run.error_message = error_message[:500]
    gather_run.completed_at = datetime.now(timezone.utc)
    gather_run.notification_status = "not_required"
    await db.flush()
    return gather_run
