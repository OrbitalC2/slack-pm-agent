"""
Suggestion approval service — approve/reject/edit suggestions.

Handles Slack interactive button clicks from gather-info results.
Approved decisions are also saved to memory_chunks per decision #5.
"""

import json
import uuid

from pm_agent.app.db.memory_queries import (
    insert_clarification,
    mark_clarification_answered,
)
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.db.suggestion_queries import (
    claim_pending_suggestion,
    get_pending_tasks_for_gather_run,
    get_suggestion_by_id,
    update_suggestion_content,
    update_suggestion_status,
)
from pm_agent.app.db.task_queries import insert_task
from pm_agent.app.db.project_queries import get_project_by_id
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.memory_service import embed_and_store
from pm_agent.app.services.slack_service import (
    open_modal,
    send_channel_message,
    send_dm,
    send_ephemeral,
    update_message,
)
from pm_agent.app.utils.permissions import (
    APPROVAL_PERMISSION_DENIED,
    can_approve_suggestion,
)
from pm_agent.app.utils.slack_formatting import (
    clarification_answer_modal,
    edit_suggestion_modal,
    task_approved_blocks,
    text_block,
)

logger = get_logger("approval")
VALID_PRIORITIES = {"low", "medium", "high", "urgent"}


async def approve_task_suggestion(action: dict, body: dict, client) -> None:
    """Approve a task suggestion — creates a task record and posts to internal channel."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion, project = await _claim_suggestion(
            db, suggestion_id, user_id, client, body
        )
        if not suggestion:
            return

        payload = suggestion.payload or {}
        task = await insert_task(
            db,
            project_id=suggestion.project_id,
            title=suggestion.title,
            description=suggestion.body,
            owner_slack_id=payload.get("suggested_owner_slack_id"),
            priority=_clean_priority(payload.get("priority")),
            source_message_ids=suggestion.source_message_ids,
            source_refs=suggestion.source_refs,
            source_suggestion_id=suggestion.id,
        )
        await db.commit()

        # Post to internal channel
        if project.internal_channel_id:
            try:
                blocks = task_approved_blocks(
                    {
                        "title": task.title,
                        "description": task.description,
                        "owner_slack_id": task.owner_slack_id,
                        "priority": task.priority,
                        "payload": payload,
                    },
                    project.name,
                )
                response = await send_channel_message(
                    client, project.internal_channel_id,
                    text=f"New task: {task.title}", blocks=blocks,
                )
                task.internal_slack_message_ts = response.get("ts")
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to post approved task to internal channel: {e}", exc_info=True)

        from pm_agent.app.services.canvas_service import sync_project_canvas

        await sync_project_canvas(project.id, reason="task_approved", client=client)
        await _mark_action_message_resolved(
            client,
            action,
            body,
            f"🟢 *Approved* by <@{user_id}>",
        )


async def approve_clarification_suggestion(action: dict, body: dict, client) -> None:
    """Send a clarification to the client — creates a pending clarification record."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion, _project = await _claim_suggestion(
            db, suggestion_id, user_id, client, body
        )
        if not suggestion:
            return

        payload = dict(suggestion.payload or {})
        payload.setdefault("answer_owner", "client")
        await insert_clarification(
            db,
            project_id=suggestion.project_id,
            question=suggestion.title,
            context=suggestion.body,
            source_message_ids=suggestion.source_message_ids,
            source_refs=suggestion.source_refs,
            source_suggestion_id=suggestion.id,
            payload=payload,
        )
        await db.commit()
        from pm_agent.app.services.canvas_service import sync_project_canvas

        await sync_project_canvas(
            suggestion.project_id, reason="clarification_approved", client=client
        )
        await _mark_action_message_resolved(
            client,
            action,
            body,
            f"🟢 *Ask Client selected* by <@{user_id}>",
        )


async def open_clarification_answer_modal(action: dict, body: dict, client) -> None:
    """Open a modal for an admin to answer a clarification internally."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])
    trigger_id = body["trigger_id"]

    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion = await get_suggestion_by_id(db, suggestion_id)
        if not suggestion:
            return
        channel_id, message_ts = _interaction_message_target(body)
        view = clarification_answer_modal({
            "id": suggestion.id,
            "title": suggestion.title,
            "payload": suggestion.payload,
            "message_channel_id": channel_id,
            "message_ts": message_ts,
        })
    await open_modal(client, trigger_id, view)


async def answer_clarification_option(action: dict, body: dict, client) -> None:
    """Save a listed clarification option as the confirmed internal answer."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])
    option_index = int(value.get("option_index", 0))

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion, project = await _claim_suggestion(
            db, suggestion_id, user_id, client, body
        )
        if not suggestion:
            return

        payload = dict(suggestion.payload or {})
        options = [str(o).strip() for o in (payload.get("options") or []) if str(o).strip()]
        if option_index < 0 or option_index >= len(options):
            await send_dm(client, user_id, text="❌ Selected clarification option was not found.")
            return
        final_answer = options[option_index]
        payload["answer_owner"] = "internal"
        clarification = await insert_clarification(
            db,
            project_id=suggestion.project_id,
            question=suggestion.title,
            context=suggestion.body,
            source_message_ids=suggestion.source_message_ids,
            source_refs=suggestion.source_refs,
            source_suggestion_id=suggestion.id,
            payload=payload,
        )
        await mark_clarification_answered(
            db, clarification.id, final_answer, answered_by_slack_id=user_id
        )
        clarification_id = clarification.id
        question = suggestion.title
        project_id = suggestion.project_id
        submitter = payload.get("submitter_slack_id")
        internal_channel_id = project.internal_channel_id if project else None
        await db.commit()

    await _after_internal_clarification_answer(
        client=client,
        project_id=project_id,
        clarification_id=clarification_id,
        question=question,
        final_answer=final_answer,
        answered_by=user_id,
        submitter=submitter,
        internal_channel_id=internal_channel_id,
    )
    await _mark_action_message_resolved(
        client,
        action,
        body,
        f"🟢 *Answered with option {chr(65 + option_index)}* by <@{user_id}>: {final_answer}",
    )


async def handle_clarification_answer_submission(view: dict, body: dict, client) -> None:
    """Save an internally-provided clarification answer as a confirmed decision."""
    user_id = body["user"]["id"]
    metadata = json.loads(view.get("private_metadata", "{}"))
    suggestion_id = uuid.UUID(metadata["suggestion_id"])
    values = view["state"]["values"]
    final_answer = (
        values["clarification_answer"]["clarification_answer_input"].get("value") or ""
    ).strip()

    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return

    if not final_answer:
        await send_dm(client, user_id, text="❌ No answer provided.")
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion, project = await _claim_suggestion(
            db, suggestion_id, user_id, client, body
        )
        if not suggestion:
            return

        payload = dict(suggestion.payload or {})
        payload["answer_owner"] = "internal"
        clarification = await insert_clarification(
            db,
            project_id=suggestion.project_id,
            question=suggestion.title,
            context=suggestion.body,
            source_message_ids=suggestion.source_message_ids,
            source_refs=suggestion.source_refs,
            source_suggestion_id=suggestion.id,
            payload=payload,
        )
        await mark_clarification_answered(
            db, clarification.id, final_answer, answered_by_slack_id=user_id
        )
        clarification_id = clarification.id
        question = suggestion.title
        project_id = suggestion.project_id
        submitter = payload.get("submitter_slack_id")
        internal_channel_id = project.internal_channel_id if project else None
        await db.commit()

    await _after_internal_clarification_answer(
        client=client,
        project_id=project_id,
        clarification_id=clarification_id,
        question=question,
        final_answer=final_answer,
        answered_by=user_id,
        submitter=submitter,
        internal_channel_id=internal_channel_id,
    )
    await _replace_modal_source_message(
        client,
        metadata,
        f"🟢 *Answered internally* by <@{user_id}>: {final_answer}",
    )


async def approve_decision_suggestion(action: dict, body: dict, client) -> None:
    """Approve a decision — marks as approved and saves to memory (decision #5)."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion, _project = await _claim_suggestion(
            db, suggestion_id, user_id, client, body
        )
        if not suggestion:
            return
        await db.commit()

    from pm_agent.app.services.canvas_service import sync_project_canvas

    await sync_project_canvas(
        suggestion.project_id, reason="decision_approved", client=client
    )

    # Save decision to memory for semantic retrieval
    await embed_and_store(
        project_id=suggestion.project_id,
        text=f"Approved decision: {suggestion.title}",
        source_type="decision",
        source_id=suggestion.id,
        importance=5,
    )
    await _mark_action_message_resolved(
        client,
        action,
        body,
        f"🟢 *Approved* by <@{user_id}>",
    )


async def acknowledge_scope_risk(action: dict, body: dict, client) -> None:
    """Acknowledge a scope risk and save it to memory."""
    await _acknowledge_memory_suggestion(
        action,
        body,
        client,
        source_type="scope_risk",
        memory_prefix="Acknowledged scope risk",
        success_label="Scope risk acknowledged",
    )


async def acknowledge_assumption(action: dict, body: dict, client) -> None:
    """Acknowledge an assumption and save it to memory."""
    await _acknowledge_memory_suggestion(
        action,
        body,
        client,
        source_type="assumption",
        memory_prefix="Acknowledged unconfirmed assumption",
        success_label="Assumption acknowledged",
    )


async def reject_suggestion(action: dict, body: dict, client) -> None:
    """Reject a suggestion — no task/clarification created."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])

    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion = await get_suggestion_by_id(db, suggestion_id)
        if not suggestion:
            return

        await update_suggestion_status(db, suggestion.id, "rejected", user_id)
        await db.commit()
        await _mark_action_message_resolved(
            client,
            action,
            body,
            f"🔴 *Declined* by <@{user_id}>",
        )


async def approve_all_tasks(action: dict, body: dict, client) -> None:
    """Approve all pending task suggestions from a gather run."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    gather_run_id = uuid.UUID(value["gather_run_id"])

    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        pending = await get_pending_tasks_for_gather_run(db, gather_run_id)
        if not pending:
            await send_dm(client, user_id, text="ℹ️ No pending tasks to approve.")
            return

        project = await get_project_by_id(db, pending[0].project_id)
        if not can_approve_suggestion(user_id):
            await _send_approval_denied(client, body, user_id)
            return

        count = 0
        approved_tasks: list[tuple] = []
        for suggestion in pending:
            claimed = await claim_pending_suggestion(db, suggestion.id, user_id)
            if not claimed:
                continue
            payload = suggestion.payload or {}
            task = await insert_task(
                db, project_id=claimed.project_id, title=claimed.title,
                description=claimed.body, owner_slack_id=payload.get("suggested_owner_slack_id"),
                priority=_clean_priority(payload.get("priority")), source_message_ids=claimed.source_message_ids,
                source_refs=claimed.source_refs,
                source_suggestion_id=claimed.id,
            )
            approved_tasks.append((task, payload))
            count += 1

        await db.commit()

        for task, payload in approved_tasks:
            if project.internal_channel_id:
                try:
                    blocks = task_approved_blocks(
                        {
                            "title": task.title,
                            "description": task.description,
                            "owner_slack_id": task.owner_slack_id,
                            "priority": task.priority,
                            "payload": payload,
                        },
                        project.name,
                    )
                    resp = await send_channel_message(client, project.internal_channel_id,
                        text=f"New task: {task.title}", blocks=blocks)
                    task.internal_slack_message_ts = resp.get("ts")
                except Exception as e:
                    logger.error(f"Failed to post approved task to internal channel: {e}", exc_info=True)

        if approved_tasks:
            await db.commit()
        from pm_agent.app.services.canvas_service import sync_project_canvas

        await sync_project_canvas(project.id, reason="tasks_approved", client=client)
        await _mark_approve_all_message_resolved(
            client,
            action,
            body,
            user_id,
            count,
        )


async def open_edit_modal(action: dict, body: dict, client) -> None:
    """Open modal for editing a suggestion."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])
    trigger_id = body["trigger_id"]

    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion = await get_suggestion_by_id(db, suggestion_id)
        if not suggestion:
            return
        view = edit_suggestion_modal({
            "id": suggestion.id, "title": suggestion.title,
            "body": suggestion.body, "payload": suggestion.payload,
        })
        from pm_agent.app.services.slack_service import open_modal as open_m
        await open_m(client, trigger_id, view)


async def handle_edit_modal_submission(view: dict, body: dict, client) -> None:
    """Handle edit modal submission — update suggestion and keep it pending."""
    user_id = body["user"]["id"]
    metadata = json.loads(view.get("private_metadata", "{}"))
    suggestion_id = uuid.UUID(metadata["suggestion_id"])

    values = view["state"]["values"]
    new_title = values["edit_title"]["edit_title_input"].get("value", "")
    new_body = values["edit_body"]["edit_body_input"].get("value", "")
    priority_sel = values.get("edit_priority", {}).get("edit_priority_input", {})
    new_priority = priority_sel.get("selected_option", {}).get("value", "medium") if priority_sel else "medium"

    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion = await get_suggestion_by_id(db, suggestion_id)
        if not suggestion:
            return
        if suggestion.status != "pending":
            await send_dm(client, user_id, text=f"ℹ️ Suggestion already {suggestion.status}.")
            return

        await update_suggestion_content(
            db, suggestion.id, title=new_title, body=new_body,
            payload={**(suggestion.payload or {}), "priority": _clean_priority(new_priority)},
        )
        await db.commit()

        await send_dm(client, user_id, text=f"✏️ Suggestion updated and still pending approval: *{new_title}*")


async def _claim_suggestion(
    db,
    suggestion_id: uuid.UUID,
    user_id: str,
    client,
    body: dict | None = None,
):
    """Verify permission and atomically claim a pending suggestion."""
    if not can_approve_suggestion(user_id):
        await _send_approval_denied(client, body, user_id)
        return None, None

    existing = await get_suggestion_by_id(db, suggestion_id)
    if not existing:
        await send_dm(client, user_id, text="❌ Suggestion not found.")
        return None, None

    project = await get_project_by_id(db, existing.project_id)
    if project is None:
        await send_dm(client, user_id, text="❌ Project not found for this suggestion.")
        return None, None
    claimed = await claim_pending_suggestion(db, suggestion_id, user_id)
    if not claimed:
        await send_dm(client, user_id, text="ℹ️ Suggestion already processed.")
        return None, project

    return claimed, project


async def _acknowledge_memory_suggestion(
    action: dict,
    body: dict,
    client,
    source_type: str,
    memory_prefix: str,
    success_label: str,
) -> None:
    """Approve a non-task/non-clarification suggestion and save it to memory."""
    user_id = body["user"]["id"]
    value = json.loads(action["value"])
    suggestion_id = uuid.UUID(value["suggestion_id"])

    session_factory = get_session_factory()
    async with session_factory() as db:
        suggestion, _project = await _claim_suggestion(
            db, suggestion_id, user_id, client, body
        )
        if not suggestion:
            return
        await db.commit()

    from pm_agent.app.services.canvas_service import sync_project_canvas

    await sync_project_canvas(
        suggestion.project_id, reason=f"{source_type}_acknowledged", client=client
    )

    details = f": {suggestion.body}" if suggestion.body else ""
    await embed_and_store(
        project_id=suggestion.project_id,
        text=f"{memory_prefix}: {suggestion.title}{details}",
        source_type=source_type,
        source_id=suggestion.id,
        importance=4,
    )
    await _mark_action_message_resolved(
        client,
        action,
        body,
        f"🟢 *{success_label}* by <@{user_id}>",
    )


async def _after_internal_clarification_answer(
    *,
    client,
    project_id,
    clarification_id,
    question: str,
    final_answer: str,
    answered_by: str,
    submitter: str | None,
    internal_channel_id: str | None,
) -> None:
    from pm_agent.app.services.canvas_service import sync_project_canvas

    await sync_project_canvas(project_id, reason="clarification_answered", client=client)

    await embed_and_store(
        project_id=project_id,
        text=f"Resolved clarification — Q: {question}\nA: {final_answer}",
        source_type="clarification",
        source_id=clarification_id,
        importance=5,
    )

    # Notify the internal channel and the original submitter (no client message).
    if internal_channel_id:
        try:
            await send_channel_message(
                client,
                internal_channel_id,
                text=f"✅ Clarification answered: {question}",
                blocks=[
                    text_block(f"✅ *Clarification answered*\n*Q:* {question}\n*A:* {final_answer}"),
                ],
            )
        except Exception as e:
            logger.error(f"Failed to post answered clarification to internal channel: {e}", exc_info=True)

    if submitter and submitter != answered_by:
        await send_dm(
            client,
            submitter,
            text=f"✅ Your clarification was answered: *{question}*\n{final_answer}",
        )


def _clean_priority(priority: str | None) -> str:
    """Return a DB-safe task priority."""
    if priority in VALID_PRIORITIES:
        return priority
    return "medium"


async def _mark_action_message_resolved(
    client,
    action: dict,
    body: dict,
    status_text: str,
) -> None:
    """Replace the clicked action row with a terminal status in the original message."""
    channel_id, message_ts = _interaction_message_target(body)
    if not channel_id or not message_ts:
        return

    original_blocks = (body.get("message") or {}).get("blocks") or []
    blocks = _resolved_message_blocks(original_blocks, action, status_text)
    try:
        await update_message(
            client,
            channel_id,
            message_ts,
            text=_plain_status_text(status_text),
            blocks=blocks,
        )
    except Exception as e:
        logger.warning(f"Failed to update suggestion action message: {e}")


async def _mark_approve_all_message_resolved(
    client,
    action: dict,
    body: dict,
    user_id: str,
    count: int,
) -> None:
    """Replace approve-all and every task action row in the clicked message."""
    channel_id, message_ts = _interaction_message_target(body)
    if not channel_id or not message_ts:
        return

    original_blocks = (body.get("message") or {}).get("blocks") or []
    blocks = _approve_all_resolved_blocks(
        original_blocks,
        action,
        all_status=f"🟢 *Approved {count} tasks* by <@{user_id}>",
        task_status=f"🟢 *Approved* by <@{user_id}>",
    )
    try:
        await update_message(
            client,
            channel_id,
            message_ts,
            text=f"Approved {count} tasks by <@{user_id}>",
            blocks=blocks,
        )
    except Exception as e:
        logger.warning(f"Failed to update approve-all message: {e}")


async def _replace_modal_source_message(
    client,
    metadata: dict,
    status_text: str,
) -> None:
    """Replace the source message for a modal-driven choice when metadata has a target."""
    channel_id = metadata.get("message_channel_id")
    message_ts = metadata.get("message_ts")
    if not channel_id or not message_ts:
        return
    try:
        await update_message(
            client,
            channel_id,
            message_ts,
            text=_plain_status_text(status_text),
            blocks=[text_block(status_text)],
        )
    except Exception as e:
        logger.warning(f"Failed to update clarification source message: {e}")


def _resolved_message_blocks(blocks: list[dict], action: dict, status_text: str) -> list[dict]:
    """Return message blocks with the matching action block replaced by status text."""
    if not blocks:
        return [text_block(status_text)]

    resolved = []
    replaced = False
    for block in blocks:
        if (
            block.get("type") == "actions"
            and not replaced
            and _actions_block_matches(block, action)
        ):
            resolved.append(text_block(status_text))
            replaced = True
            continue
        resolved.append(block)

    if not replaced:
        resolved.append(text_block(status_text))
    return resolved


def _approve_all_resolved_blocks(
    blocks: list[dict],
    action: dict,
    all_status: str,
    task_status: str,
) -> list[dict]:
    if not blocks:
        return [text_block(all_status)]

    resolved = []
    replaced_all = False
    for block in blocks:
        if block.get("type") == "actions" and _actions_block_matches(block, action):
            resolved.append(text_block(all_status))
            replaced_all = True
            continue
        if _is_task_action_block(block):
            resolved.append(text_block(task_status))
            continue
        resolved.append(block)

    if not replaced_all:
        resolved.append(text_block(all_status))
    return resolved


def _is_task_action_block(block: dict) -> bool:
    if block.get("type") != "actions":
        return False
    return any(
        element.get("action_id") == "approve_suggestion_task"
        for element in block.get("elements") or []
    )


def _actions_block_matches(block: dict, action: dict) -> bool:
    action_block_id = action.get("block_id")
    if action_block_id and block.get("block_id") == action_block_id:
        return True

    action_value = action.get("value")
    action_id = action.get("action_id")
    for element in block.get("elements") or []:
        if action_value and element.get("value") == action_value:
            return True
        if not action_value and action_id and element.get("action_id") == action_id:
            return True
    return False


def _interaction_message_target(body: dict | None) -> tuple[str | None, str | None]:
    if not body:
        return None, None
    container = body.get("container") or {}
    channel_id = container.get("channel_id")
    message_ts = container.get("message_ts")

    if not channel_id:
        channel_id = (body.get("channel") or {}).get("id") or body.get("channel_id")
    if not message_ts:
        message_ts = (body.get("message") or {}).get("ts")

    return channel_id, message_ts


def _plain_status_text(status_text: str) -> str:
    return status_text.replace("*", "")


async def _send_approval_denied(client, body: dict | None, user_id: str) -> None:
    """Send the approval denial privately for button or modal contexts."""
    channel_id = _interaction_channel_id(body)
    if channel_id:
        await send_ephemeral(
            client,
            channel_id,
            user_id,
            APPROVAL_PERMISSION_DENIED,
        )
        return
    await send_dm(client, user_id, text=APPROVAL_PERMISSION_DENIED)


def _interaction_channel_id(body: dict | None) -> str | None:
    if not body:
        return None
    channel = body.get("channel") or {}
    if channel.get("id"):
        return channel["id"]
    container = body.get("container") or {}
    if container.get("channel_id"):
        return container["channel_id"]
    return body.get("channel_id")
