"""
Huddle notes service — manual huddle note extraction.

Accepts rough notes in Urdu/English/Roman Urdu, extracts structured
decisions/tasks/clarifications, sends suggestions for project manager approval.
"""

import asyncio
import json

from pm_agent.app.agents.chains import huddle_notes_chain
from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.db.huddle_queries import insert_huddle_run
from pm_agent.app.db.project_queries import find_project_by_channel
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.db.suggestion_queries import insert_suggestion
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.memory_service import embed_and_store
from pm_agent.app.services.slack_service import open_modal, send_dm
from pm_agent.app.utils.permissions import (
    can_approve_suggestion,
    get_global_admin_slack_ids,
    is_internal_project_channel,
)
from pm_agent.app.utils.priorities import clean_priority
from pm_agent.app.utils.slack_ids import looks_like_slack_user_id
from pm_agent.app.utils.source_refs import huddle_run_source_ref
from pm_agent.app.utils.slack_formatting import (
    huddle_notes_modal,
    huddle_summary_block_batches,
)

logger = get_logger("huddle_notes")
HUDDLE_INTERNAL_ONLY = "❌ `/agent huddle-notes` is only available in the internal project channel."


async def open_huddle_modal(
    client,
    trigger_id: str,
    channel_id: str,
    user_id: str | None = None,
) -> None:
    """Open the huddle notes input modal."""
    if user_id:
        session_factory = get_session_factory()
        async with session_factory() as db:
            project = await find_project_by_channel(db, channel_id)
            if not project:
                await send_dm(client, user_id, text="❌ No project configured for this channel.")
                return
            if not is_internal_project_channel(project, channel_id):
                await send_dm(client, user_id, text=HUDDLE_INTERNAL_ONLY)
                return

    view = huddle_notes_modal(channel_id)
    await open_modal(client, trigger_id, view)


async def handle_huddle_modal_submission(view: dict, body: dict, client) -> None:
    """Handle huddle notes modal submission — extract and create suggestions."""
    user_id = body["user"]["id"]
    metadata = json.loads(view.get("private_metadata", "{}"))
    channel_id = metadata.get("channel_id")

    values = view["state"]["values"]
    notes_text = values["huddle_notes"]["huddle_notes_input"].get("value", "")
    participants = values.get("participants", {}).get("participants_input", {}).get("value")

    if not notes_text.strip():
        await send_dm(client, user_id, text="❌ No huddle notes provided.")
        return

    # Run extraction in background
    asyncio.create_task(
        _process_huddle_notes(user_id, channel_id, notes_text, participants, client)
    )
    await send_dm(client, user_id, text="📝 Processing huddle notes. I'll send you the results shortly.")


async def _process_huddle_notes(
    user_id: str,
    channel_id: str,
    notes_text: str,
    participants: str | None,
    client,
) -> None:
    """Process huddle notes — extract, create suggestions, notify project manager."""
    session_factory = get_session_factory()

    try:
        async with session_factory() as db:
            project = await find_project_by_channel(db, channel_id)
            if not project:
                await send_dm(client, user_id, text="❌ No project configured for this channel.")
                return
            if not is_internal_project_channel(project, channel_id):
                await send_dm(client, user_id, text=HUDDLE_INTERNAL_ONLY)
                return

            # Run LLM extraction
            llm_client = get_llm_client()
            output = await huddle_notes_chain(notes_text, llm_client, participants)

            huddle_run = await insert_huddle_run(
                db,
                project_id=project.id,
                channel_id=channel_id,
                submitted_by_slack_id=user_id,
                raw_notes=notes_text,
                participants=participants,
                summary=output.summary or None,
            )
            huddle_source_refs = [huddle_run_source_ref(huddle_run.id, user_id)]
            huddle_run_id = huddle_run.id

            # Create suggestions from extracted items
            all_suggestions = []

            for dec in output.decisions:
                s = await insert_suggestion(
                    db, project_id=project.id, suggestion_type="decision",
                    title=dec.decision, body=dec.source_context,
                    source_refs=huddle_source_refs,
                    payload={
                        "source_type": "huddle_run",
                        "submitted_by_slack_id": user_id,
                    },
                )
                all_suggestions.append({
                    "id": s.id, "suggestion_type": "decision",
                    "title": s.title, "body": s.body,
                    "source_message_ids": [], "source_refs": s.source_refs,
                    "gather_run_id": "",
                })

            for task in output.tasks:
                suggested_owner = (
                    task.assigned_to if looks_like_slack_user_id(task.assigned_to) else None
                )
                s = await insert_suggestion(
                    db, project_id=project.id, suggestion_type="task",
                    title=task.title, body=task.description,
                    source_refs=huddle_source_refs,
                    payload={
                        "priority": clean_priority(task.priority),
                        "assignee_text": task.assigned_to,
                        "suggested_owner_slack_id": suggested_owner,
                        "source_type": "huddle_run",
                        "submitted_by_slack_id": user_id,
                    },
                )
                all_suggestions.append({
                    "id": s.id, "suggestion_type": "task",
                    "title": s.title, "body": s.body,
                    "source_message_ids": [], "source_refs": s.source_refs,
                    "gather_run_id": "",
                })

            for clar in output.clarifications:
                s = await insert_suggestion(
                    db, project_id=project.id, suggestion_type="clarification",
                    title=clar.question, body=clar.context,
                    source_refs=huddle_source_refs,
                    payload={
                        "source_type": "huddle_run",
                        "submitted_by_slack_id": user_id,
                    },
                )
                all_suggestions.append({
                    "id": s.id, "suggestion_type": "clarification",
                    "title": s.title, "body": s.body,
                    "source_message_ids": [], "source_refs": s.source_refs,
                    "gather_run_id": "",
                })

            await db.commit()

            from pm_agent.app.services.canvas_service import sync_project_canvas

            await sync_project_canvas(
                project.id, reason="huddle_processed", client=client
            )

            # Store huddle summary in memory after the source record commits.
            if output.summary:
                await embed_and_store(
                    project_id=project.id,
                    text=f"Huddle summary: {output.summary}",
                    source_type="huddle_summary",
                    source_id=huddle_run_id,
                    importance=4,
                )

            # Send approval buttons only to admins; dev submitters get a private summary.
            batches = huddle_summary_block_batches(
                project.name,
                output.summary,
                all_suggestions,
            )
            if can_approve_suggestion(user_id):
                await _send_huddle_approval_batches(client, user_id, project.name, batches)
            else:
                admin_ids = get_global_admin_slack_ids()
                for admin_id in admin_ids:
                    await _send_huddle_approval_batches(
                        client, admin_id, project.name, batches
                    )
                if admin_ids:
                    text = (
                        f"Huddle notes processed for {project.name}. "
                        "Suggestions were sent to admins for approval."
                    )
                else:
                    text = (
                        f"Huddle notes processed for {project.name}. "
                        "No global admins are configured to receive approval suggestions."
                    )
                await send_dm(client, user_id, text=text)

    except Exception as e:
        logger.error(f"Huddle notes processing failed: {e}", exc_info=True)
        await send_dm(client, user_id, text=f"❌ Huddle notes processing failed: {str(e)[:200]}")


async def _send_huddle_approval_batches(
    client,
    user_id: str,
    project_name: str,
    batches: list[list[dict]],
) -> None:
    """DM Slack-safe huddle approval batches to one admin."""
    total = len(batches)
    for idx, blocks in enumerate(batches, 1):
        suffix = f" ({idx}/{total})" if total > 1 else ""
        await send_dm(
            client,
            user_id,
            text=f"Huddle notes processed for {project_name}{suffix}",
            blocks=blocks,
        )
