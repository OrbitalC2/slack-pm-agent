"""
Project service — handles project setup, status, updates, and blockers.
"""

import json
import re
from datetime import datetime, timezone

from pm_agent.app.db.project_queries import (
    create_project,
    find_project_by_channel,
    find_project_by_client_channel,
    find_project_by_internal_channel,
    get_project_by_id,
    update_project,
)
from pm_agent.app.db.task_queries import get_task_summary, get_blocked_tasks
from pm_agent.app.db.memory_queries import get_pending_clarifications
from pm_agent.app.db.suggestion_queries import get_approved_suggestions
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.slack_service import open_modal, send_dm
from pm_agent.app.utils.permissions import (
    COMMAND_PERMISSION_DENIED,
    is_global_admin,
    is_internal_project_channel,
)
from pm_agent.app.utils.channel_refs import extract_channel_id, extract_channel_name
from pm_agent.app.utils.project_update_draft import (
    build_project_update_data,
    client_safe_decisions,
    format_fallback_project_update,
    format_project_update_draft,
)
from pm_agent.app.utils.slack_formatting import setup_project_modal

logger = get_logger("project_service")


async def setup_project(
    user_id: str,
    channel_id: str,
    args_text: str,
    client,
) -> str:
    """
    Handle /agent setup-project with inline arguments.

    Example: name="Invoice Dashboard" client="Example Client" internal="#internal-project-x"
    """
    args = _parse_setup_args(args_text)

    name = args.get("name")
    if not name:
        return '❌ Project name is required. Example: `/agent setup-project name="Project X"`'

    client_name = args.get("client")
    client_channel = args.get("client_channel") or args.get("client-channel")
    internal_channel = (
        args.get("internal_channel")
        or args.get("internal-channel")
        or args.get("internal")
    )
    github_url = args.get("github")

    if not client_channel:
        return (
            "❌ `client_channel` is required for inline setup. Example: "
            '`/agent setup-project name="Project X" client_channel="#client-project-x"`'
        )

    client_channel_id = await _resolve_channel_ref(client, client_channel)
    if not client_channel_id:
        return f"❌ Could not resolve client channel `{client_channel}`."

    internal_channel_id = None
    internal_channel_was_provided = internal_channel is not None
    if internal_channel:
        internal_channel_id = await _resolve_channel_ref(client, internal_channel)
        if not internal_channel_id:
            return f"❌ Could not resolve internal channel `{internal_channel}`."
    elif channel_id != client_channel_id:
        internal_channel_id = channel_id

    if internal_channel_id and internal_channel_id == client_channel_id:
        return "❌ Client and internal channels must be different."

    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            project, updated, error = await _save_project(
                db=db,
                user_id=user_id,
                current_channel_id=channel_id,
                name=name,
                client_channel_id=client_channel_id,
                client_name=client_name,
                internal_channel_id=internal_channel_id,
                internal_channel_was_provided=(
                    internal_channel_was_provided or internal_channel_id is not None
                ),
                github_url=github_url,
            )
            if error:
                await db.rollback()
                return error
            await db.commit()
            from pm_agent.app.services.canvas_service import sync_project_canvas

            await sync_project_canvas(project.id, reason="project_setup", client=client)
            return _project_confirmation(project, updated=updated)

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating project: {e}", exc_info=True)
            return f"❌ Error creating project: {str(e)}"


async def open_setup_modal(client, trigger_id: str, channel_id: str) -> None:
    """Open the project setup modal."""
    view = setup_project_modal(channel_id)
    await open_modal(client, trigger_id, view)


async def handle_setup_modal_submission(view: dict, body: dict, client) -> None:
    """Handle project setup modal form submission."""
    user_id = body["user"]["id"]
    metadata = json.loads(view.get("private_metadata", "{}"))
    channel_id = metadata.get("channel_id")

    values = view["state"]["values"]
    name = values["project_name"]["project_name_input"].get("value", "")
    client_name = values["client_name"]["client_name_input"].get("value")
    github_url = values["github_repo"]["github_repo_input"].get("value")

    client_selection = values.get("client_channel", {}).get("client_channel_input", {})
    client_channel_id = client_selection.get("selected_conversation")
    if not client_channel_id:
        await send_dm(client, user_id, text="❌ Select the client channel before saving the project.")
        return

    # Internal channel from conversation_select
    internal_selection = values["internal_channel"]["internal_channel_input"]
    internal_channel_id = internal_selection.get("selected_conversation")

    if internal_channel_id and internal_channel_id == client_channel_id:
        await send_dm(client, user_id, text="❌ Client and internal channels must be different.")
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            project, updated, error = await _save_project(
                db=db,
                user_id=user_id,
                current_channel_id=channel_id,
                name=name,
                client_channel_id=client_channel_id,
                client_name=client_name,
                internal_channel_id=internal_channel_id,
                internal_channel_was_provided=internal_channel_id is not None,
                github_url=github_url,
            )
            if error:
                await db.rollback()
                await send_dm(client, user_id, text=error)
                return
            await db.commit()

            from pm_agent.app.services.canvas_service import sync_project_canvas

            await sync_project_canvas(project.id, reason="project_setup", client=client)

            confirmation = _project_confirmation(project, updated=updated)
            await send_dm(client, user_id, text=confirmation)

        except Exception as e:
            await db.rollback()
            logger.error(f"Error in setup modal: {e}", exc_info=True)
            await send_dm(client, user_id, text=f"❌ Error creating project: {str(e)}")


async def get_project_status(user_id: str, channel_id: str) -> str:
    """Generate project status report."""
    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            return "❌ No project configured for this channel. Run `/agent setup-project` first."
        if not is_internal_project_channel(project, channel_id):
            return "❌ Project status is only available in the internal project channel."

        task_summary = await get_task_summary(db, project.id)
        pending_clarifications = await get_pending_clarifications(db, project.id)

        lines = [
            f"📊 *Project Status: {project.name}*\n",
            f"*Tasks:* {task_summary['total']} total",
            f"  • Todo: {task_summary['todo']}",
            f"  • In Progress: {task_summary['doing']}",
            f"  • Blocked: {task_summary['blocked']}",
            f"  • In Review: {task_summary['review']}",
            f"  • Done: {task_summary['done']}",
            "",
            f"*Pending Clarifications:* {len(pending_clarifications)}",
        ]

        for c in pending_clarifications[:5]:
            lines.append(f"  • {c.question}")

        if project.project_summary:
            lines.append(f"\n*Last Scope Summary:*\n{project.project_summary[:500]}")

        return "\n".join(lines)


async def get_project_blockers(user_id: str, channel_id: str) -> str:
    """Generate blockers report."""
    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            return "❌ No project configured for this channel."
        if not is_internal_project_channel(project, channel_id):
            return "❌ Blockers are only available in the internal project channel."

        blocked_tasks = await get_blocked_tasks(db, project.id)
        pending_clarifications = await get_pending_clarifications(db, project.id)

        if not blocked_tasks and not pending_clarifications:
            return f"✅ *{project.name}*: No blockers or pending clarifications."

        lines = [f"🚧 *Blockers: {project.name}*\n"]

        if blocked_tasks:
            lines.append("*Blocked Tasks:*")
            for t in blocked_tasks:
                owner = f"<@{t.owner_slack_id}>" if t.owner_slack_id else "Unassigned"
                lines.append(f"  • {t.title} ({owner})")

        if pending_clarifications:
            lines.append("\n*Pending Clarifications:*")
            for c in pending_clarifications:
                lines.append(f"  • {c.question}")

        return "\n".join(lines)


async def generate_project_update(user_id: str, channel_id: str) -> None:
    """Generate client-ready draft update (sent privately to project manager)."""
    from pm_agent.app.agents.chains import project_update_chain
    from pm_agent.app.agents.llm_client import get_llm_client
    from pm_agent.app.services.github_service import get_github_summary
    from pm_agent.app.services.slack_service import get_slack_client

    client = get_slack_client()

    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            await send_dm(client, user_id, text="❌ No project configured for this channel.")
            return
        if not is_global_admin(user_id):
            await send_dm(client, user_id, text=COMMAND_PERMISSION_DENIED)
            return

        task_summary = await get_task_summary(db, project.id)
        pending_clarifications = await get_pending_clarifications(db, project.id)
        approved_decisions = client_safe_decisions(
            await get_approved_suggestions(db, project.id, "decision")
        )
        approved_risks = await get_approved_suggestions(db, project.id, "scope_risk")
        approved_assumptions = await get_approved_suggestions(db, project.id, "assumption")
        project_name = project.name
        project_id = project.id
        github_repo_url = project.github_repo_url

    github_summary = ""
    if github_repo_url:
        try:
            github_summary = await get_github_summary(github_repo_url)
        except Exception as e:
            logger.warning(f"GitHub summary failed for {github_repo_url}: {e}", exc_info=True)
            github_summary = "GitHub activity could not be fetched."

    project_data = build_project_update_data(
        project_name=project_name,
        task_summary=task_summary,
        pending_clarifications=pending_clarifications,
        approved_decisions=approved_decisions,
        approved_risks=approved_risks,
        approved_assumptions=approved_assumptions,
        github_summary=github_summary,
    )

    try:
        llm_client = get_llm_client()
        update_output = await project_update_chain(project_data, llm_client)
        draft = format_project_update_draft(project_name, update_output)
    except Exception as e:
        logger.warning(f"Project update LLM draft failed: {e}", exc_info=True)
        draft = format_fallback_project_update(
            project_name=project_name,
            task_summary=task_summary,
            pending_clarifications=pending_clarifications,
            github_summary=github_summary,
        )

    try:
        async with session_factory() as db:
            project = await get_project_by_id(db, project_id)
            if project is not None:
                await update_project(
                    db,
                    project,
                    latest_client_update_draft=draft,
                    latest_client_update_generated_at=datetime.now(timezone.utc),
                )
                await db.commit()

        from pm_agent.app.services.canvas_service import sync_project_canvas

        await sync_project_canvas(project_id, reason="project_update", client=client)
    except Exception as e:
        logger.error(
            f"Could not persist/sync latest project update for {project_id}: {e}",
            exc_info=True,
        )

    await send_dm(client, user_id, text=draft)


# ── Private Helpers ──


async def _save_project(
    db,
    user_id: str,
    current_channel_id: str,
    name: str,
    client_channel_id: str,
    client_name: str | None,
    internal_channel_id: str | None,
    internal_channel_was_provided: bool,
    github_url: str | None,
):
    """Create or update a project while guarding channel collisions."""
    existing = await _find_existing_project_for_setup(
        db,
        current_channel_id=current_channel_id,
        client_channel_id=client_channel_id,
        internal_channel_id=internal_channel_id,
    )

    if isinstance(existing, str):
        return None, False, existing

    target_internal_channel_id = internal_channel_id
    if existing and not internal_channel_was_provided:
        target_internal_channel_id = existing.internal_channel_id
    if target_internal_channel_id and target_internal_channel_id == client_channel_id:
        return None, False, "❌ Client and internal channels must be different."

    if existing:
        if not is_global_admin(user_id):
            return None, False, COMMAND_PERMISSION_DENIED

        canvas_reset = {}
        if existing.internal_channel_id != target_internal_channel_id:
            canvas_reset = {
                "internal_canvas_id": None,
                "internal_canvas_updated_at": None,
                "internal_canvas_status": "not_created",
            }

        project = await update_project(
            db,
            existing,
            name=name,
            client_channel_id=client_channel_id,
            client_name=client_name if client_name is not None else existing.client_name,
            internal_channel_id=target_internal_channel_id,
            github_repo_url=github_url if github_url is not None else existing.github_repo_url,
            **canvas_reset,
        )
        return project, True, None

    if not is_global_admin(user_id):
        return None, False, COMMAND_PERMISSION_DENIED

    project = await create_project(
        db,
        name=name,
        client_channel_id=client_channel_id,
        project_manager_slack_id=user_id,
        client_name=client_name,
        internal_channel_id=internal_channel_id,
        github_repo_url=github_url,
        created_from_channel_id=current_channel_id,
    )
    return project, False, None


async def _find_existing_project_for_setup(
    db,
    current_channel_id: str,
    client_channel_id: str,
    internal_channel_id: str | None,
):
    """Find the existing project touched by setup, or return an error string."""
    candidates = []
    for project in (
        await find_project_by_channel(db, current_channel_id),
        await find_project_by_client_channel(db, client_channel_id),
        await find_project_by_internal_channel(db, client_channel_id),
        await find_project_by_client_channel(db, internal_channel_id) if internal_channel_id else None,
        await find_project_by_internal_channel(db, internal_channel_id) if internal_channel_id else None,
    ):
        if project and all(existing.id != project.id for existing in candidates):
            candidates.append(project)

    if len(candidates) > 1:
        return (
            "❌ The selected client/internal channels already belong to different "
            "projects. Pick unused channels or update one project at a time."
        )

    return candidates[0] if candidates else None


def _parse_setup_args(text: str) -> dict:
    """Parse key=value or key="value with spaces" arguments."""
    args = {}
    # Match key="value" or key=value patterns
    pattern = r'(\w+)\s*=\s*"([^"]*)"|\b(\w+)\s*=\s*(\S+)'
    for match in re.finditer(pattern, text):
        if match.group(1):
            args[match.group(1)] = match.group(2)
        elif match.group(3):
            args[match.group(3)] = match.group(4)
    return args


async def _resolve_channel_ref(client, channel_ref: str) -> str | None:
    """Resolve a Slack channel reference from ID, mention, or #name."""
    channel_id = extract_channel_id(channel_ref)
    if channel_id:
        return channel_id

    channel_name = extract_channel_name(channel_ref)
    if channel_name:
        return await _resolve_channel_id(client, channel_name)

    return None


async def _resolve_channel_id(client, channel_name: str) -> str | None:
    """Try to resolve a #channel-name to a channel ID."""
    clean_name = channel_name.lstrip("#")
    try:
        cursor = None
        while True:
            response = await client.conversations_list(
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor,
            )
            for ch in response.get("channels", []):
                if ch["name"] == clean_name:
                    return ch["id"]
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        logger.warning(f"Could not resolve channel name '{channel_name}': {e}")
    return None


def _project_confirmation(project, updated: bool = False) -> str:
    """Build project confirmation message."""
    action = "updated" if updated else "saved"
    lines = [
        f"✅ Project {action}.",
        f"*Name:* {project.name}",
    ]
    if project.client_name:
        lines.append(f"*Client:* {project.client_name}")
    lines.append(f"*Client channel:* <#{project.client_channel_id}>")
    if project.internal_channel_id:
        lines.append(f"*Internal channel:* <#{project.internal_channel_id}>")
    if project.github_repo_url:
        lines.append(f"*GitHub:* {project.github_repo_url}")
    lines.append(
        "\n_Next step: discuss scope with the client. When ready, run `/agent gather-info`._"
    )
    return "\n".join(lines)
