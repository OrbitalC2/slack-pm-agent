"""Best-effort internal Slack Canvas dashboard synchronization."""

import asyncio
import re
import uuid
from datetime import datetime, timezone

from slack_sdk.errors import SlackApiError

from pm_agent.app.db.document_queries import get_recent_documents_with_chunk_counts
from pm_agent.app.db.memory_queries import get_clarifications
from pm_agent.app.db.project_queries import get_project_by_id, update_project
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.db.suggestion_queries import get_approved_suggestions
from pm_agent.app.db.task_queries import get_tasks
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.slack_service import (
    create_channel_canvas,
    edit_canvas,
    get_slack_client,
)

logger = get_logger("canvas_service")

TASK_LIMIT = 20
BLOCKED_TASK_LIMIT = 15
REQUIREMENT_LIMIT = 20
QA_LIMIT = 15
CLIENT_CLARIFICATION_LIMIT = 15
DECISION_LIMIT = 10
DOCUMENT_LIMIT = 10
SUMMARY_LIMIT = 1500
DOC_SUMMARY_LIMIT = 600
ITEM_TEXT_LIMIT = 240

_project_locks: dict[uuid.UUID, asyncio.Lock] = {}


async def ensure_project_canvas(project_id: uuid.UUID, client=None) -> bool:
    """Ensure an internal project Canvas exists and contains current state."""
    return await sync_project_canvas(project_id, reason="ensure", client=client)


async def render_project_canvas(project_id: uuid.UUID) -> str:
    """Render the complete internal, dev-facing dashboard from PostgreSQL state."""
    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await get_project_by_id(db, project_id)
        if project is None:
            raise LookupError(f"Project {project_id} was not found.")

        tasks = await get_tasks(db, project_id)
        clarifications = await get_clarifications(db, project_id)
        task_suggestions = await get_approved_suggestions(db, project_id, "task")
        decisions = await get_approved_suggestions(db, project_id, "decision")
        documents = await get_recent_documents_with_chunk_counts(
            db, project_id, limit=DOCUMENT_LIMIT + 1
        )

    return _render_dashboard(
        project=project,
        tasks=tasks,
        clarifications=clarifications,
        task_suggestions=task_suggestions,
        decisions=decisions,
        documents=documents,
    )


async def sync_project_canvas(
    project_id: uuid.UUID,
    reason: str,
    client=None,
) -> bool:
    """Synchronize one project Canvas without raising into the caller."""
    lock = _project_locks.setdefault(project_id, asyncio.Lock())
    async with lock:
        try:
            return await _sync_project_canvas(
                project_id, reason=reason, client=client or get_slack_client()
            )
        except Exception as exc:
            logger.error(
                f"Canvas sync failed for project {project_id} ({reason}): {exc}",
                exc_info=True,
            )
            await _persist_canvas_state(project_id, status="failed")
            return False


async def _sync_project_canvas(project_id: uuid.UUID, reason: str, client) -> bool:
    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await get_project_by_id(db, project_id)
        if project is None:
            return False
        if not project.internal_channel_id:
            return False
        if project.internal_channel_id == project.client_channel_id:
            raise RuntimeError("Refusing to create a Canvas in the client channel.")
        canvas_id = project.internal_canvas_id

    markdown = await render_project_canvas(project_id)
    if canvas_id:
        try:
            await edit_canvas(client, canvas_id, markdown)
        except SlackApiError as exc:
            if exc.response.get("error") != "canvas_not_found":
                raise
            await _persist_canvas_state(
                project_id,
                status="not_created",
                canvas_id=None,
                updated_at=None,
                replace_canvas_id=True,
            )
            canvas_id = await _create_internal_channel_canvas(
                project_id, client, markdown
            )
    else:
        canvas_id = await _create_internal_channel_canvas(project_id, client, markdown)

    await _persist_canvas_state(
        project_id,
        status="created",
        canvas_id=canvas_id,
        updated_at=datetime.now(timezone.utc),
        replace_canvas_id=True,
    )
    logger.info(f"Canvas synchronized for project {project_id} ({reason})")
    return True


async def _create_internal_channel_canvas(
    project_id: uuid.UUID,
    client,
    markdown: str,
) -> str:
    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await get_project_by_id(db, project_id)
        if project is None or not project.internal_channel_id:
            raise RuntimeError("Project has no internal channel for Canvas creation.")
        if project.internal_channel_id == project.client_channel_id:
            raise RuntimeError("Refusing to create a Canvas in the client channel.")
        channel_id = project.internal_channel_id
        title = _truncate(f"{project.name} Project Dashboard", 100)

    canvas_id, adopted = await create_channel_canvas(
        client, channel_id, title, markdown
    )
    if adopted:
        await edit_canvas(client, canvas_id, markdown)
    return canvas_id


async def _persist_canvas_state(
    project_id: uuid.UUID,
    status: str,
    canvas_id: str | None = None,
    updated_at: datetime | None = None,
    replace_canvas_id: bool = False,
) -> None:
    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            project = await get_project_by_id(db, project_id)
            if project is None:
                return
            values = {"internal_canvas_status": status}
            if replace_canvas_id:
                values["internal_canvas_id"] = canvas_id
                values["internal_canvas_updated_at"] = updated_at
            elif updated_at is not None:
                values["internal_canvas_updated_at"] = updated_at
            await update_project(db, project, **values)
            await db.commit()
    except Exception as exc:
        logger.error(
            f"Could not persist Canvas state for project {project_id}: {exc}",
            exc_info=True,
        )


def _render_dashboard(
    project,
    tasks: list,
    clarifications: list,
    task_suggestions: list,
    decisions: list,
    documents: list[tuple[object, int]],
) -> str:
    """Render the dev-facing dashboard.

    This view is internal/operational. It intentionally excludes admin-sensitive
    content (client reply drafts, pricing/negotiation/scope risks). Only
    implementation blockers are surfaced for developers.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    github = f"[Repository]({project.github_repo_url})" if project.github_repo_url else "-"
    internal_channel = f"<#{project.internal_channel_id}>" if project.internal_channel_id else "-"
    task_suggestion_by_id = {str(item.id): item for item in task_suggestions}

    lines = [
        f"# {_plain(project.name)} Project Dashboard",
        "",
        "## Project Overview",
        f"- **GitHub:** {github}",
        f"- **Internal channel:** {internal_channel}",
        f"- **Last updated:** {now}",
        "",
        "## Project Summary",
        _truncate((project.project_summary or "").strip(), SUMMARY_LIMIT)
        or "_No summary yet._",
    ]

    _render_confirmed_requirements(lines, documents)
    _render_open_tasks(lines, tasks, task_suggestion_by_id)
    _render_blocked_tasks(lines, tasks, task_suggestion_by_id)
    _render_developer_qa(lines, clarifications)
    _render_pending_client_clarifications(lines, clarifications)
    _render_implementation_decisions(lines, decisions)
    _render_documents(lines, documents)

    return "\n".join(lines).strip() + "\n"


def _render_confirmed_requirements(lines: list[str], documents) -> None:
    """Source confirmed requirements from parsed document analysis items."""
    lines.extend(["", "## Confirmed Requirements"])
    seen = set()
    rendered = []
    for document, _chunk_count in documents:
        permalink = document.permalink
        for item in (document.analysis_items or {}).get("requirements", []):
            text = item.get("text") if isinstance(item, dict) else str(item)
            text = _plain(text)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            suffix = f" ([source]({permalink}))" if permalink else ""
            rendered.append(f"- {_truncate(text, ITEM_TEXT_LIMIT)}{suffix}")
    if rendered:
        lines.extend(rendered[:REQUIREMENT_LIMIT])
        _append_limit_notice(lines, len(rendered), REQUIREMENT_LIMIT)
    else:
        lines.append("_No confirmed requirements captured yet._")


def _render_open_tasks(lines: list[str], tasks, task_suggestion_by_id) -> None:
    lines.extend(["", "## Open Tasks"])
    open_tasks = [
        task for task in tasks if task.status not in ("done", "blocked")
    ]
    open_tasks.sort(key=_task_sort_key)
    if not open_tasks:
        lines.append("_No open tasks._")
        return
    for task in open_tasks[:TASK_LIMIT]:
        source_suggestion = task_suggestion_by_id.get(str(task.source_suggestion_id))
        payload = getattr(source_suggestion, "payload", None) or {}
        owner = _task_owner(task, source_suggestion)
        source = _source_link(task.source_refs)
        header = (
            f"- **[{task.priority.title()}] {_plain(task.title)}** — "
            f"{task.status.title()} · Owner: {owner}"
        )
        if source != "-":
            header += f" · {source}"
        lines.append(header)
        criteria = [c for c in (payload.get("acceptance_criteria") or []) if str(c).strip()]
        if criteria:
            brief = "; ".join(_plain(c) for c in criteria[:3])
            lines.append(f"  - Acceptance: {_truncate(brief, ITEM_TEXT_LIMIT)}")
        deps = [d for d in (payload.get("dependencies") or []) if str(d).strip()]
        if deps:
            lines.append(f"  - Depends on: {_truncate('; '.join(_plain(d) for d in deps), ITEM_TEXT_LIMIT)}")
    _append_limit_notice(lines, len(open_tasks), TASK_LIMIT)


def _render_blocked_tasks(lines: list[str], tasks, task_suggestion_by_id) -> None:
    lines.extend(["", "## Blocked Tasks"])
    blocked = []
    for task in tasks:
        if task.status == "done":
            continue
        payload = getattr(
            task_suggestion_by_id.get(str(task.source_suggestion_id)), "payload", None
        ) or {}
        blockers = [b for b in (payload.get("blocked_by") or []) if str(b).strip()]
        if task.status == "blocked" or blockers:
            blocked.append((task, blockers))
    if not blocked:
        lines.append("_No blocked tasks._")
        return
    for task, blockers in blocked[:BLOCKED_TASK_LIMIT]:
        lines.append(f"- **{_plain(task.title)}** ({task.status.title()})")
        if blockers:
            lines.append(
                f"  - Blocked by: {_truncate('; '.join(_plain(b) for b in blockers), ITEM_TEXT_LIMIT)}"
            )
            lines.append("  - Needed: a decision/answer to unblock this task.")
        else:
            lines.append("  - Needed: confirm the blocker and the action required to unblock.")
    _append_limit_notice(lines, len(blocked), BLOCKED_TASK_LIMIT)


def _is_developer_clarification(clarification) -> bool:
    payload = clarification.payload or {}
    return bool(payload.get("submitter_slack_id")) or payload.get("source_type") == "dev_clarify"


def _render_developer_qa(lines: list[str], clarifications) -> None:
    lines.extend(["", "## Developer Clarifications / Q&A"])
    pending_dev = [
        c for c in clarifications
        if c.status == "pending" and _is_developer_clarification(c)
    ]
    answered = [c for c in clarifications if c.status == "answered"]
    pending_dev.sort(key=lambda item: item.created_at)
    answered.sort(key=lambda item: item.answered_at or item.created_at, reverse=True)

    if not pending_dev and not answered:
        lines.append("_No developer clarifications yet._")
        return

    if pending_dev:
        lines.append("**Pending developer questions:**")
        for clar in pending_dev[:QA_LIMIT]:
            owner = (clar.payload or {}).get("answer_owner") or "admin"
            lines.append(f"- {_truncate(_plain(clar.question), ITEM_TEXT_LIMIT)} _(needs answer from: {owner})_")
        _append_limit_notice(lines, len(pending_dev), QA_LIMIT)

    if answered:
        lines.append("**Answered questions:**")
        for clar in answered[:QA_LIMIT]:
            answer = _truncate(_plain(clar.final_answer), ITEM_TEXT_LIMIT) or "(answer recorded)"
            who = f" — answered by <@{clar.answered_by_slack_id}>" if clar.answered_by_slack_id else ""
            source = _source_link(clar.source_refs)
            source_suffix = f" {source}" if source != "-" else ""
            lines.append(f"- **Q:** {_truncate(_plain(clar.question), ITEM_TEXT_LIMIT)}")
            lines.append(f"  - **A:** {answer}{who}{source_suffix}")
        _append_limit_notice(lines, len(answered), QA_LIMIT)


def _render_pending_client_clarifications(lines: list[str], clarifications) -> None:
    lines.extend(["", "## Pending Client Clarifications"])
    pending_client = [
        c for c in clarifications
        if c.status == "pending"
        and not _is_developer_clarification(c)
        and ((c.payload or {}).get("answer_owner", "client") == "client")
    ]
    pending_client.sort(key=lambda item: item.created_at)
    if not pending_client:
        lines.append("_No pending client clarifications._")
        return
    for clar in pending_client[:CLIENT_CLARIFICATION_LIMIT]:
        payload = clar.payload or {}
        lines.append(f"- {_truncate(_plain(clar.question), ITEM_TEXT_LIMIT)}")
        options = [o for o in (payload.get("options") or []) if str(o).strip()]
        if options:
            lines.append(f"  - Options: {_truncate('; '.join(_plain(o) for o in options), ITEM_TEXT_LIMIT)}")
        recommended = _plain(payload.get("recommended_option"))
        if recommended:
            lines.append(f"  - Recommended: {_truncate(recommended, ITEM_TEXT_LIMIT)}")
        lines.append(f"  - Status: {clar.status.title()}")
    _append_limit_notice(lines, len(pending_client), CLIENT_CLARIFICATION_LIMIT)


def _render_implementation_decisions(lines: list[str], decisions) -> None:
    lines.extend(["", "## Recent Implementation Decisions"])
    recent_decisions = sorted(decisions, key=lambda item: item.created_at, reverse=True)
    if not recent_decisions:
        lines.append("_No confirmed decisions yet._")
        return
    for decision in recent_decisions[:DECISION_LIMIT]:
        prefix = "[Internal] " if (decision.payload or {}).get("channel_kind") == "internal" else ""
        lines.append(f"- {prefix}{_truncate(_plain(decision.title), ITEM_TEXT_LIMIT)}")
    _append_limit_notice(lines, len(recent_decisions), DECISION_LIMIT)


def _render_documents(lines: list[str], documents) -> None:
    lines.extend(["", "## Documents"])
    if not documents:
        lines.append("_No documents tracked yet._")
        return
    for document, chunk_count in documents[:DOCUMENT_LIMIT]:
        name = document.title or document.filename or document.slack_file_id
        link = f" — [Open in Slack]({document.permalink})" if document.permalink else ""
        lines.append(f"### {_plain(name)}{link}")
        status = (
            f"Parse: {document.parse_status} · Analysis: {document.analysis_status}"
        )
        if document.page_count is not None:
            status += f" · Pages: {document.page_count}"
        status += f" · Chunks: {chunk_count}"
        lines.append(status)

        if document.analysis_summary:
            lines.append("")
            lines.append("Summary:")
            lines.append(_truncate(_plain(document.analysis_summary), DOC_SUMMARY_LIMIT))

        items = document.analysis_items or {}
        _render_doc_item_list(lines, items, "suggested_tasks", "title", "Implementation points")
        _render_doc_item_list(lines, items, "requirements", "text", "Requirements")
        _render_doc_item_list(lines, items, "clarifications", "question", "Unresolved questions")
        lines.append("")
    _append_limit_notice(lines, len(documents), DOCUMENT_LIMIT)


def _render_doc_item_list(lines, items, key, field, label) -> None:
    values = []
    for item in (items.get(key) or []):
        primary = item.get(field) if isinstance(item, dict) else str(item)
        primary = _plain(primary)
        if primary:
            values.append(primary)
    if not values:
        return
    lines.append(f"{label}:")
    for value in values[:5]:
        lines.append(f"- {_truncate(value, ITEM_TEXT_LIMIT)}")


def _task_sort_key(task) -> tuple:
    priority = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    status = {"blocked": 0, "doing": 1, "review": 2, "todo": 3}
    return (
        priority.get(task.priority, 9),
        status.get(task.status, 9),
        task.created_at,
    )


def _task_owner(task, source_suggestion) -> str:
    if task.owner_slack_id:
        return f"<@{task.owner_slack_id}>"
    payload = getattr(source_suggestion, "payload", None) or {}
    return payload.get("assignee_text") or payload.get("owner_hint") or "Unassigned"


def _source_link(source_refs) -> str:
    for ref in source_refs or []:
        permalink = ref.get("permalink") if isinstance(ref, dict) else None
        if permalink:
            return f"[Source]({permalink})"
    return "-"


def _append_limit_notice(lines: list[str], total: int, limit: int) -> None:
    if total > limit:
        lines.append(f"_Showing top {limit} items. Use /agent status for more._")


def _plain(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _table_cell(value, limit: int) -> str:
    return _truncate(_plain(value), limit).replace("|", "\\|") or "-"


def _truncate(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
