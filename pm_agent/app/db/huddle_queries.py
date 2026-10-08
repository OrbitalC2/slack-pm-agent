"""
Database queries for huddle_runs.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import HuddleRun


async def insert_huddle_run(
    db: AsyncSession,
    project_id: uuid.UUID,
    channel_id: str,
    submitted_by_slack_id: str,
    raw_notes: str,
    participants: str | None = None,
    summary: str | None = None,
) -> HuddleRun:
    """Create a huddle run provenance record."""
    huddle_run = HuddleRun(
        project_id=project_id,
        channel_id=channel_id,
        submitted_by_slack_id=submitted_by_slack_id,
        raw_notes=raw_notes,
        participants=participants,
        summary=summary,
    )
    db.add(huddle_run)
    await db.flush()
    return huddle_run
