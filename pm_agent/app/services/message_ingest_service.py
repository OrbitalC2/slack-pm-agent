"""
Message ingest service — saves raw Slack messages from configured project channels.

Follows spec §6.3:
- Does NOT run LLM extraction on every message
- Does NOT post in the client channel automatically
- Ignores unconfigured channels
- Uses channel-based sender type detection (decision #7)
"""

from pm_agent.app.db.message_queries import (
    get_message_by_channel_ts,
    insert_message,
    mark_message_deleted,
    update_message_content,
)
from pm_agent.app.db.project_queries import find_project_by_channel
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.logging_config import get_logger
from pm_agent.app.utils.text_cleaning import (
    message_text_with_file_placeholders,
    normalize_slack_text,
)

logger = get_logger("message_ingest")

# Subtypes to ignore (not relevant for content extraction)
IGNORED_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "group_topic",
    "group_purpose",
    "group_name",
    "group_archive",
    "group_unarchive",
    "bot_add",
    "bot_remove",
    "pinned_item",
    "unpinned_item",
    "tombstone",
    "thread_broadcast",  # Duplicate of thread message
}


async def ingest_message(event: dict, context: dict) -> None:
    """
    Process an incoming Slack message event.

    - Idempotently stores messages via channel/timestamp uniqueness
    - Ignores unconfigured channels
    - Determines sender type based on channel and user
    - Saves raw message to messages table
    """
    channel_id = event.get("channel")
    message_ts = event.get("ts")
    subtype = event.get("subtype")
    user_id = event.get("user")
    bot_id = event.get("bot_id")
    text = message_text_with_file_placeholders(event)
    thread_ts = event.get("thread_ts")
    document_files = list(event.get("files") or [])

    # Skip irrelevant subtypes
    if subtype in IGNORED_SUBTYPES:
        logger.debug(f"Ignoring message subtype '{subtype}' in {channel_id}")
        return

    # If it's a message_changed subtype, extract the actual message
    if subtype == "message_changed":
        inner = event.get("message", {})
        text = message_text_with_file_placeholders(inner)
        user_id = inner.get("user")
        bot_id = inner.get("bot_id")
        message_ts = inner.get("ts", message_ts)
        thread_ts = inner.get("thread_ts")
        document_files = list(inner.get("files") or [])

    deleted_ts = None
    if subtype == "message_deleted":
        deleted_ts = (
            event.get("deleted_ts")
            or event.get("previous_message", {}).get("ts")
            or message_ts
        )

    # Skip messages with no text content unless this is a delete event.
    if not text and not event.get("attachments") and not document_files and not deleted_ts:
        return

    session_factory = get_session_factory()
    async with session_factory() as db:
        try:
            # Find project for this channel
            project = await find_project_by_channel(db, channel_id)
            if not project:
                # Channel not configured for any project — ignore
                return

            if deleted_ts:
                await mark_message_deleted(db, channel_id, deleted_ts)
                await db.commit()
                logger.debug(f"Marked deleted message in {channel_id} (ts={deleted_ts})")
                return

            # Determine if sender is a bot
            is_bot = bool(bot_id) or (subtype == "bot_message")

            # Determine sender type using channel-based detection (decision #7)
            sender_type = _detect_sender_type(
                user_id=user_id,
                is_bot=is_bot,
                channel_id=channel_id,
                project=project,
            )

            # Normalize text for cleaner retrieval
            normalized = normalize_slack_text(text) if text else None

            if subtype == "message_changed":
                updated = await update_message_content(
                    db,
                    slack_channel_id=channel_id,
                    slack_message_ts=message_ts,
                    raw_text=text or "",
                    normalized_text=normalized,
                    sender_slack_id=user_id,
                    sender_type=sender_type,
                    is_bot=is_bot,
                    slack_thread_ts=thread_ts,
                    message_subtype="message_changed",
                )
                if updated:
                    await db.commit()
                    logger.debug(f"Updated edited message in {channel_id} (ts={message_ts})")
                    if document_files:
                        from pm_agent.app.services.document_service import register_message_documents

                        await register_message_documents(
                            project_id=project.id,
                            channel_id=channel_id,
                            source_message_id=updated.id,
                            files=document_files,
                        )
                    return

            # Save message (idempotent via unique constraint)
            stored_message = await insert_message(
                db,
                project_id=project.id,
                slack_channel_id=channel_id,
                slack_message_ts=message_ts,
                raw_text=text or "",
                sender_slack_id=user_id,
                sender_type=sender_type,
                is_bot=is_bot,
                slack_thread_ts=thread_ts,
                message_subtype=subtype,
                normalized_text=normalized,
            )

            if stored_message is None:
                stored_message = await get_message_by_channel_ts(db, channel_id, message_ts)

            await db.commit()
            logger.debug(
                f"Saved message from {sender_type} in {channel_id} (ts={message_ts})"
            )
            if document_files and stored_message is not None:
                from pm_agent.app.services.document_service import register_message_documents

                await register_message_documents(
                    project_id=project.id,
                    channel_id=channel_id,
                    source_message_id=stored_message.id,
                    files=document_files,
                )

        except Exception as e:
            await db.rollback()
            logger.error(f"Error ingesting message: {e}", exc_info=True)
            raise


def _detect_sender_type(
    user_id: str | None,
    is_bot: bool,
    channel_id: str,
    project,
) -> str:
    """
    Detect sender type using channel-based logic (decision #7):

    - If sender is bot -> bot
    - If sender is project.project_manager_slack_id -> project_manager
    - If message is in client_channel_id and sender is not project_manager/bot -> client
    - If message is in internal_channel_id and sender is not project_manager/bot -> developer
    - Otherwise -> unknown
    """
    if is_bot:
        return "bot"

    if user_id and user_id == project.project_manager_slack_id:
        return "project_manager"

    if channel_id == project.client_channel_id:
        return "client"

    if channel_id == project.internal_channel_id:
        return "developer"

    return "unknown"
