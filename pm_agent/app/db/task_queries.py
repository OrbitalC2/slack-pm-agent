"""
Database queries for the tasks table.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from pm_agent.app.db.models import Task


async def insert_task(
    db: AsyncSession,
    project_id: uuid.UUID,
    title: str,
    description: str | None = None,
    owner_slack_id: str | None = None,
    priority: str = "medium",
    source_message_ids: list | None = None,
    source_refs: list | None = None,
    source_suggestion_id: uuid.UUID | None = None,
) -> Task:
    """Create a new task (status=todo)."""
    task = Task(
        project_id=project_id,
        title=title,
        description=description,
        owner_slack_id=owner_slack_id,
        priority=priority,
        source_message_ids=source_message_ids or [],
        source_refs=source_refs or [],
        source_suggestion_id=source_suggestion_id,
    )
    db.add(task)
    await db.flush()
    return task


async def get_task_by_id(db: AsyncSession, task_id: uuid.UUID) -> Task | None:
    """Get a task by ID."""
    return await db.get(Task, task_id)


async def get_tasks(
    db: AsyncSession,
    project_id: uuid.UUID,
    status: str | None = None,
) -> list[Task]:
    """Get tasks for a project, optionally filtered by status."""
    conditions = [Task.project_id == project_id]
    if status:
        conditions.append(Task.status == status)

    result = await db.execute(
        select(Task)
        .where(and_(*conditions))
        .order_by(Task.created_at.asc())
    )
    return list(result.scalars().all())


async def get_blocked_tasks(db: AsyncSession, project_id: uuid.UUID) -> list[Task]:
    """Get blocked tasks for a project."""
    return await get_tasks(db, project_id, status="blocked")


async def update_task_status(
    db: AsyncSession,
    task_id: uuid.UUID,
    status: str,
) -> Task | None:
    """Update a task's status."""
    task = await get_task_by_id(db, task_id)
    if task is None:
        return None

    task.status = status
    task.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return task


async def update_task(
    db: AsyncSession,
    task: Task,
    **kwargs,
) -> Task:
    """Update task fields."""
    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)
    task.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return task


async def get_task_summary(db: AsyncSession, project_id: uuid.UUID) -> dict:
    """Get a summary of tasks by status for a project."""
    tasks = await get_tasks(db, project_id)

    summary = {
        "total": len(tasks),
        "todo": 0,
        "doing": 0,
        "blocked": 0,
        "review": 0,
        "done": 0,
        "tasks": tasks,
    }

    for task in tasks:
        if task.status in summary:
            summary[task.status] += 1

    return summary
