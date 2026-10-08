"""
Database queries for the projects table.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import Project


async def create_project(
    db: AsyncSession,
    name: str,
    client_channel_id: str,
    project_manager_slack_id: str,
    client_name: str | None = None,
    internal_channel_id: str | None = None,
    github_repo_url: str | None = None,
    created_from_channel_id: str | None = None,
) -> Project:
    """Create a new project record."""
    project = Project(
        name=name,
        client_name=client_name,
        client_channel_id=client_channel_id,
        internal_channel_id=internal_channel_id,
        project_manager_slack_id=project_manager_slack_id,
        github_repo_url=github_repo_url,
        created_from_channel_id=created_from_channel_id,
    )
    db.add(project)
    await db.flush()
    return project


async def get_project_by_id(db: AsyncSession, project_id: uuid.UUID) -> Project | None:
    """Get project by ID."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    return result.scalar_one_or_none()


async def find_project_by_channel(
    db: AsyncSession, channel_id: str
) -> Project | None:
    """
    Find a project by client or internal channel ID.
    Returns the project that has this channel as either client_channel_id
    or internal_channel_id.
    """
    result = await db.execute(
        select(Project).where(
            or_(
                Project.client_channel_id == channel_id,
                Project.internal_channel_id == channel_id,
            )
        )
    )
    return result.scalar_one_or_none()


async def find_project_by_client_channel(
    db: AsyncSession, channel_id: str
) -> Project | None:
    """Find project by client channel ID only."""
    result = await db.execute(
        select(Project).where(Project.client_channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def find_project_by_internal_channel(
    db: AsyncSession, channel_id: str
) -> Project | None:
    """Find project by internal channel ID only."""
    result = await db.execute(
        select(Project).where(Project.internal_channel_id == channel_id)
    )
    return result.scalar_one_or_none()


async def update_project(
    db: AsyncSession,
    project: Project,
    **kwargs,
) -> Project:
    """Update project fields."""
    for key, value in kwargs.items():
        if hasattr(project, key):
            setattr(project, key, value)
    project.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return project
