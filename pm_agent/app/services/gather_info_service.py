"""
Gather-info service — the 18-step pipeline from spec §5.2.1.

Triggered by /agent gather-info. Runs as a background task.
"""

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from pm_agent.app.agents.chains import gather_info_chain, merge_gather_outputs
from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.db.gather_queries import (
    get_latest_completed_gather_for_channel,
    mark_gather_completed,
    mark_gather_failed,
    update_gather_notification_status,
)
from pm_agent.app.db.document_queries import (
    get_documents_for_messages,
    get_unconsumed_completed_documents,
    mark_documents_consumed,
)
from pm_agent.app.db.message_queries import (
    bulk_insert_messages,
    get_message_by_channel_ts,
    get_messages_since,
    update_message_permalink,
)
from pm_agent.app.db.models import GatherRun
from pm_agent.app.db.project_queries import find_project_by_channel, update_project
from pm_agent.app.db.suggestion_queries import insert_suggestion
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.memory_service import embed_and_store
from pm_agent.app.services.slack_service import fetch_channel_history, get_permalink, send_dm
from pm_agent.app.utils.permissions import (
    COMMAND_PERMISSION_DENIED,
    can_run_command,
    get_project_channel_kind,
)
from pm_agent.app.utils.gather_sources import (
    collect_output_source_ids,
    sanitize_gather_output_sources,
)
from pm_agent.app.utils.document_context import format_document_context
from pm_agent.app.utils.priorities import clean_priority
from pm_agent.app.utils.slack_formatting import gather_summary_block_batches
from pm_agent.app.utils.source_refs import (
    document_source_refs,
    slack_message_source_ref,
    source_refs_for_message_ids,
)
from pm_agent.app.utils.text_cleaning import (
    format_transcript,
    message_text_with_file_placeholders,
    normalize_slack_text,
)

logger = get_logger("gather_info")
MAX_TRANSCRIPT_CHARS = 30000
MAX_DOCUMENT_CONTEXT_PER_MESSAGE_CHARS = 12000
# Synthetic transcript "message id" used to carry document analysis that
# completed after its original upload message was already gathered.
CARRYOVER_DOCUMENTS_SOURCE_ID = "carryover-documents"


async def run_gather_info(user_id: str, channel_id: str, client) -> None:
    """Run the full gather-info pipeline (background task)."""
    session_factory = get_session_factory()
    gather_run_id = None
    project_name = "project"

    # Step 2-4: Find project, verify permissions, create gather run
    async with session_factory() as db:
        try:
            project = await find_project_by_channel(db, channel_id)
            if not project:
                await send_dm(client, user_id, text="❌ No project configured. Run `/agent setup-project` first.")
                return
            if not can_run_command(user_id, "gather-info"):
                await send_dm(client, user_id, text=COMMAND_PERMISSION_DENIED)
                return
            channel_kind = get_project_channel_kind(project, channel_id)
            if channel_kind is None:
                await send_dm(client, user_id, text="❌ This channel is not configured for the project.")
                return

            latest_gather = await get_latest_completed_gather_for_channel(
                db, project.id, channel_id
            )
            last_gathered_ts = latest_gather.to_slack_ts if latest_gather else None

            gather_run = GatherRun(
                project_id=project.id, started_by_slack_id=user_id,
                slack_channel_id=channel_id, from_slack_ts=last_gathered_ts,
                status="running", notification_status="pending",
            )
            db.add(gather_run)
            await db.flush()
            gather_run_id = gather_run.id
            project_id = project.id
            project_name = project.name
            gather_channel_id = channel_id
            project_manager_slack_id = project.project_manager_slack_id
            await db.commit()
        except IntegrityError as e:
            await db.rollback()
            logger.info(f"Gather already running for channel {channel_id}: {e}")
            await send_dm(
                client,
                user_id,
                text=(
                    "⏳ A gather-info run is already in progress for this project "
                    "channel. Please wait for it to finish before starting another one."
                ),
            )
            return
        except Exception as e:
            await db.rollback()
            logger.error(f"Error starting gather-info: {e}", exc_info=True)
            await send_dm(client, user_id, text=f"❌ Error: {e}")
            return

    # Steps 5-18: Main pipeline
    try:
        all_suggestions = []
        reqs = []
        memory_text = ""
        documents_considered = []

        async with session_factory() as db:
            # Step 6: Fetch messages from DB
            messages = await get_messages_since(db, project_id, gather_channel_id, last_gathered_ts)

            # Step 7: Backfill from Slack API if needed
            if not messages or last_gathered_ts is None:
                logger.info("Backfilling from Slack API...")
                try:
                    slack_msgs = await fetch_channel_history(client, gather_channel_id, oldest=last_gathered_ts, limit=500)
                    backfill = []
                    for sm in slack_msgs:
                        raw_text = message_text_with_file_placeholders(sm)
                        if not raw_text:
                            continue
                        sid = sm.get("user")
                        is_bot = bool(sm.get("bot_id"))
                        if is_bot:
                            stype = "bot"
                        elif sid == project_manager_slack_id:
                            stype = "project_manager"
                        else:
                            stype = "client" if channel_kind == "client" else "developer"
                        backfill.append({
                            "project_id": project_id, "slack_channel_id": gather_channel_id,
                            "slack_message_ts": sm["ts"], "slack_thread_ts": sm.get("thread_ts"),
                            "sender_slack_id": sid, "sender_type": stype, "is_bot": is_bot,
                            "message_subtype": sm.get("subtype"), "raw_text": raw_text,
                            "normalized_text": normalize_slack_text(raw_text),
                        })
                    if backfill:
                        await bulk_insert_messages(db, backfill)
                        await db.commit()
                    # Register documents for every Slack history message with files,
                    # even ones that already existed in the DB (e.g. from before
                    # document support), so historical file uploads are not missed.
                    await _register_history_file_documents(
                        db, project_id, gather_channel_id, slack_msgs, client
                    )
                    messages = await get_messages_since(db, project_id, gather_channel_id, last_gathered_ts)
                except Exception as e:
                    logger.warning(f"Backfill failed: {e}")

            # Step 9: Filter
            filtered = [m for m in messages if not m.is_bot and m.message_subtype not in (
                "channel_join", "channel_leave", "group_join", "group_leave", "bot_message"
            ) and (m.raw_text or "").strip()]

            # Step 10: Collect linked documents (in this window) plus carry-over
            # documents whose analysis completed after their upload message was
            # already gathered, so they are still included exactly once.
            linked_rows, carryover_rows = await _collect_gather_documents(
                db, project_id, gather_channel_id, [m.id for m in filtered]
            )
            documents_considered = _document_display_names(linked_rows, carryover_rows)

            if not filtered and not carryover_rows:
                await mark_gather_completed(
                    db, gather_run_id,
                    to_slack_ts=messages[-1].slack_message_ts if messages else None,
                    notification_status="not_required",
                )
                await db.commit()
                from pm_agent.app.services.canvas_service import sync_project_canvas

                await sync_project_canvas(
                    project_id, reason="gather_completed", client=client
                )
                if messages:
                    await _safe_send_dm(client, user_id, f"ℹ️ No relevant messages for *{project_name}*.")
                else:
                    await _safe_send_dm(client, user_id, f"ℹ️ No new messages for *{project_name}*.")
                return

            # Step 11-12: Build document context, format transcript, run LLM
            (
                document_context_by_message_id,
                document_refs_by_message_id,
                carryover_entry,
                consumed_document_ids,
            ) = _build_document_context(linked_rows, carryover_rows)

            transcript_data = [{
                "id": str(m.id), "sender_type": m.sender_type, "sender_slack_id": m.sender_slack_id,
                "raw_text": m.raw_text, "normalized_text": m.normalized_text,
                "slack_message_ts": m.slack_message_ts, "slack_thread_ts": m.slack_thread_ts,
            } for m in filtered]
            for transcript_message in transcript_data:
                contexts = document_context_by_message_id.get(transcript_message["id"], [])
                if contexts:
                    base_text = transcript_message.get("normalized_text") or transcript_message.get("raw_text") or ""
                    document_context = _compact_document_contexts(contexts)
                    transcript_message["normalized_text"] = (
                        base_text + "\n\n" + document_context
                    ).strip()
            if carryover_entry is not None:
                transcript_data.append(carryover_entry)

            transcript = format_transcript(transcript_data)
            llm_client = get_llm_client()

            if len(transcript) > MAX_TRANSCRIPT_CHARS:
                chunks = _chunk_transcript(transcript_data, MAX_TRANSCRIPT_CHARS)
                partials = [
                    await gather_info_chain(format_transcript(c), llm_client, channel_kind=channel_kind)
                    for c in chunks
                ]
                output = await merge_gather_outputs(partials, llm_client)
            else:
                output = await gather_info_chain(transcript, llm_client, channel_kind=channel_kind)

            message_by_id = {str(m.id): m for m in filtered}
            valid_source_ids = set(message_by_id)
            if carryover_entry is not None:
                valid_source_ids.add(CARRYOVER_DOCUMENTS_SOURCE_ID)
            sanitize_gather_output_sources(output, valid_source_ids)
            output_source_ids = collect_output_source_ids(output)
            source_refs_by_message_id = await _build_slack_source_refs(
                db, client, message_by_id, output_source_ids
            )

            # Step 14: Update project summary
            project = await find_project_by_channel(db, channel_id)
            if output.project_scope_summary:
                summary = output.project_scope_summary
                if project.project_summary:
                    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    summary = f"{project.project_summary}\n\n--- Gather {date_str} ---\n{summary}"
                await update_project(db, project, project_summary=summary)

            # Step 15: Store memory
            mem_parts = []
            if output.project_scope_summary:
                mem_parts.append(f"Scope: {output.project_scope_summary}")
            for r in output.confirmed_requirements:
                mem_parts.append(f"Requirement: {r.text}")
            for d in output.client_decisions:
                mem_parts.append(f"Decision: {d.decision}")
            if mem_parts:
                memory_text = "\n".join(mem_parts)

            # Step 16: Insert suggestions
            for t in output.suggested_tasks:
                source_refs = _gather_item_source_refs(t.source_message_ids, source_refs_by_message_id, document_refs_by_message_id)
                task_payload = {
                    "priority": clean_priority(t.priority),
                    "confidence": t.confidence,
                    "context": t.context,
                    "acceptance_criteria": list(t.acceptance_criteria),
                    "dependencies": list(t.dependencies),
                    "blocked_by": list(t.blocked_by),
                    "suggested_owner_slack_id": t.suggested_owner_slack_id,
                    "reason": t.reason,
                    "channel_kind": channel_kind,
                }
                s = await insert_suggestion(db, project_id, "task", t.title, t.description or t.reason,
                    gather_run_id, t.source_message_ids, source_refs, task_payload)
                all_suggestions.append({"id": s.id, "suggestion_type": "task", "title": s.title, "body": s.body, "source_message_ids": s.source_message_ids, "source_refs": s.source_refs, "payload": s.payload, "gather_run_id": str(gather_run_id)})

            for c in output.open_clarifications:
                source_refs = _gather_item_source_refs(c.source_message_ids, source_refs_by_message_id, document_refs_by_message_id)
                clar_payload = {
                    "why_it_matters": c.why_it_matters,
                    "known_context": c.known_context,
                    "options": list(c.options),
                    "recommended_option": c.recommended_option,
                    "answer_owner": c.answer_owner,
                    "confidence": c.confidence,
                    "channel_kind": channel_kind,
                }
                s = await insert_suggestion(db, project_id, "clarification", c.question, c.why_it_matters or c.known_context or "",
                    gather_run_id, c.source_message_ids, source_refs, clar_payload)
                all_suggestions.append({"id": s.id, "suggestion_type": "clarification", "title": s.title, "body": s.body, "source_message_ids": s.source_message_ids, "source_refs": s.source_refs, "payload": s.payload, "gather_run_id": str(gather_run_id)})

            for d in output.client_decisions:
                source_refs = _gather_item_source_refs(d.source_message_ids, source_refs_by_message_id, document_refs_by_message_id)
                s = await insert_suggestion(db, project_id, "decision", d.decision, "", gather_run_id, d.source_message_ids, source_refs, {"channel_kind": channel_kind})
                all_suggestions.append({"id": s.id, "suggestion_type": "decision", "title": s.title, "body": s.body, "source_message_ids": s.source_message_ids, "source_refs": s.source_refs, "payload": s.payload, "gather_run_id": str(gather_run_id)})

            for r in output.scope_risks:
                source_refs = _gather_item_source_refs(r.source_message_ids, source_refs_by_message_id, document_refs_by_message_id)
                s = await insert_suggestion(db, project_id, "scope_risk", r.risk, r.why_it_matters, gather_run_id, r.source_message_ids, source_refs, {"channel_kind": channel_kind})
                all_suggestions.append({"id": s.id, "suggestion_type": "scope_risk", "title": s.title, "body": s.body, "source_message_ids": s.source_message_ids, "source_refs": s.source_refs, "payload": s.payload, "gather_run_id": str(gather_run_id)})

            for a in output.unconfirmed_assumptions:
                source_refs = _gather_item_source_refs(a.source_message_ids, source_refs_by_message_id, document_refs_by_message_id)
                s = await insert_suggestion(db, project_id, "assumption", a.assumption, f"Needs confirmation from: {a.needs_confirmation_from}", gather_run_id, a.source_message_ids, source_refs, {"channel_kind": channel_kind})
                all_suggestions.append({"id": s.id, "suggestion_type": "assumption", "title": s.title, "body": s.body, "source_message_ids": s.source_message_ids, "source_refs": s.source_refs, "payload": s.payload, "gather_run_id": str(gather_run_id)})

            reqs = [{"text": r.text} for r in output.confirmed_requirements]

            # Step 17: Mark included completed-analysis documents consumed so the
            # same document is not re-included by a later gather run, then commit
            # DB state before Slack delivery.
            if consumed_document_ids:
                await mark_documents_consumed(db, consumed_document_ids, gather_run_id)
            latest_ts = messages[-1].slack_message_ts if messages else last_gathered_ts
            await mark_gather_completed(
                db, gather_run_id, to_slack_ts=latest_ts,
                notification_status="pending",
            )
            await db.commit()

        from pm_agent.app.services.canvas_service import sync_project_canvas

        await sync_project_canvas(project_id, reason="gather_completed", client=client)

        if memory_text:
            try:
                await embed_and_store(project_id, memory_text, "gather_summary", gather_run_id, 4)
            except Exception as e:
                logger.error(f"Non-fatal memory storage failure: {e}", exc_info=True)

        # Step 18: Send private message after DB commit; track notification separately.
        notification_status = "sent"
        try:
            batches = gather_summary_block_batches(
                project_name, output.project_scope_summary, reqs,
                all_suggestions, str(gather_run_id),
                documents=documents_considered,
            )
            total = len(batches)
            for idx, blocks in enumerate(batches, 1):
                suffix = f" ({idx}/{total})" if total > 1 else ""
                await send_dm(
                    client,
                    user_id,
                    text=f"Gather-info results for {project_name}{suffix}",
                    blocks=blocks,
                )
        except Exception as e:
            notification_status = "failed"
            logger.error(f"Gather-info DB committed but Slack DM failed: {e}", exc_info=True)
            await _safe_send_dm(
                client, user_id,
                f"⚠️ Gather-info completed for *{project_name}*, but I could not deliver the full results DM.",
            )

        try:
            async with session_factory() as db:
                await update_gather_notification_status(db, gather_run_id, notification_status)
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to update gather notification status: {e}", exc_info=True)

        logger.info(f"Gather-info completed for {project_name}: {len(all_suggestions)} suggestions")

    except Exception as e:
        logger.error(f"Gather-info failed: {e}", exc_info=True)
        try:
            async with session_factory() as db:
                if gather_run_id:
                    await mark_gather_failed(db, gather_run_id, str(e))
                    await db.commit()
        except Exception:
            pass
        await _safe_send_dm(client, user_id, f"❌ Gather-info failed: {str(e)[:200]}")


def _chunk_transcript(messages: list[dict], max_chars: int) -> list[list[dict]]:
    """Split messages into chunks fitting within max_chars."""
    chunks, current, size = [], [], 0
    for msg in messages:
        msg_size = len(msg.get("normalized_text") or msg.get("raw_text", "")) + 100
        if size + msg_size > max_chars and current:
            chunks.append(current)
            current, size = [], 0
        current.append(msg)
        size += msg_size
    if current:
        chunks.append(current)
    return chunks


def _compact_document_contexts(contexts: list[str]) -> str:
    """Cap all document analysis attached to one Slack transcript message."""
    combined = "\n\n".join(contexts)
    if len(combined) <= MAX_DOCUMENT_CONTEXT_PER_MESSAGE_CHARS:
        return combined
    marker = "\n\n[Additional document context omitted for prompt size.]"
    return combined[: MAX_DOCUMENT_CONTEXT_PER_MESSAGE_CHARS - len(marker)].rstrip() + marker


async def _collect_gather_documents(db, project_id, channel_id, filtered_message_ids):
    """Return (linked_rows, carryover_rows) for a gather window.

    linked_rows are documents attached to messages inside the current gather
    window. carryover_rows are completed, not-yet-consumed documents for the
    channel that are not already linked in this window (their upload message is
    behind the gather pointer but their analysis only finished afterwards).
    """
    linked_rows = await get_documents_for_messages(db, project_id, filtered_message_ids)
    carryover_rows = await get_unconsumed_completed_documents(db, project_id, channel_id)
    linked_ids = {document.id for document, _ in linked_rows}
    carryover_rows = [
        (document, chunk_count)
        for document, chunk_count in carryover_rows
        if document.id not in linked_ids
    ]
    return linked_rows, carryover_rows


def _document_display_names(linked_rows, carryover_rows) -> list:
    """Return de-duplicated display names for documents considered in a gather."""
    names = []
    seen = set()
    for document, _chunk_count in list(linked_rows) + list(carryover_rows):
        name = document.title or document.filename or document.slack_file_id
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _build_document_context(linked_rows, carryover_rows):
    """Build transcript document context, source refs, and consumed-document ids.

    Returns a tuple of:
        - document_context_by_message_id: per-message document context blocks
        - document_refs_by_message_id: per-message document source refs (the
          carry-over documents are keyed under CARRYOVER_DOCUMENTS_SOURCE_ID)
        - carryover_entry: a synthetic transcript message carrying carry-over
          document context, or None when there are no carry-over documents
        - consumed_document_ids: ids of completed-analysis documents that this
          gather actually includes and should mark consumed
    """
    document_context_by_message_id: dict[str, list[str]] = {}
    document_refs_by_message_id: dict[str, list[dict]] = {}
    consumed_document_ids: list = []

    for document, chunk_count in linked_rows:
        message_key = str(document.source_message_id)
        document_context_by_message_id.setdefault(message_key, []).append(
            format_document_context(document, chunk_count)
        )
        document_refs_by_message_id.setdefault(message_key, []).extend(
            document_source_refs(document)
        )
        # Only completed analyses are "consumed"; pending/failed/unsupported
        # documents stay eligible for a future gather once they complete.
        if document.analysis_status == "completed":
            consumed_document_ids.append(document.id)

    carryover_entry = None
    if carryover_rows:
        blocks = []
        for document, chunk_count in carryover_rows:
            blocks.append(format_document_context(document, chunk_count))
            document_refs_by_message_id.setdefault(
                CARRYOVER_DOCUMENTS_SOURCE_ID, []
            ).extend(document_source_refs(document))
            consumed_document_ids.append(document.id)
        carryover_entry = {
            "id": CARRYOVER_DOCUMENTS_SOURCE_ID,
            "sender_type": "system",
            "sender_slack_id": "documents",
            "raw_text": "Previously uploaded project documents (analyzed earlier).",
            "normalized_text": (
                "Previously uploaded project documents (analyzed earlier):\n\n"
                + _compact_document_contexts(blocks)
            ),
            "slack_message_ts": "",
            "slack_thread_ts": None,
        }

    return (
        document_context_by_message_id,
        document_refs_by_message_id,
        carryover_entry,
        consumed_document_ids,
    )


async def _register_history_file_documents(db, project_id, channel_id, slack_msgs, client):
    """Register documents for Slack history messages with files.

    Runs for every fetched history message that has files, even ones that
    already exist in the DB (e.g. messages stored before document support
    existed), so historical uploads are not skipped during backfill.
    """
    from pm_agent.app.services.document_service import register_message_documents

    for sm in slack_msgs:
        files = list(sm.get("files") or [])
        if not files:
            continue
        stored_message = await get_message_by_channel_ts(db, channel_id, sm.get("ts"))
        if stored_message is None:
            continue
        await register_message_documents(
            project_id=project_id,
            channel_id=channel_id,
            source_message_id=stored_message.id,
            files=files,
            client=client,
        )


async def _build_slack_source_refs(
    db,
    client,
    message_by_id: dict[str, object],
    source_ids: set[str],
) -> dict[str, dict]:
    """Build source refs and lazily persist Slack permalinks where possible."""
    refs = {}
    for source_id in source_ids:
        message = message_by_id.get(source_id)
        if not message:
            continue
        if not message.permalink:
            try:
                permalink = await get_permalink(
                    client, message.slack_channel_id, message.slack_message_ts
                )
                if permalink:
                    await update_message_permalink(db, message.id, permalink)
                    message.permalink = permalink
            except Exception as e:
                logger.warning(f"Could not resolve permalink for source message {source_id}: {e}")
        refs[source_id] = slack_message_source_ref(message)
    return refs


async def _safe_send_dm(client, user_id: str, text: str) -> None:
    """Best-effort DM for status/error notifications."""
    try:
        await send_dm(client, user_id, text=text)
    except Exception as e:
        logger.warning(f"Failed to send status DM: {e}")


def _gather_item_source_refs(
    source_message_ids: list[str],
    slack_refs_by_message_id: dict[str, dict],
    document_refs_by_message_id: dict[str, list[dict]],
) -> list[dict]:
    """Combine cited Slack-message provenance with linked document refs."""
    refs = source_refs_for_message_ids(source_message_ids, slack_refs_by_message_id)
    seen = {(ref.get("type"), ref.get("id") or ref.get("message_id")) for ref in refs}
    for message_id in source_message_ids:
        for ref in document_refs_by_message_id.get(str(message_id), []):
            key = (ref.get("type"), ref.get("id"))
            if key not in seen:
                refs.append(ref)
                seen.add(key)
    return refs
