"""
Slack event handlers — message saving and app_mention dispatch.

Keeps handlers thin. Business logic lives in service modules.
"""

from slack_bolt.async_app import AsyncApp

from pm_agent.app.logging_config import get_logger

logger = get_logger("slack_events")


def _event_type_label(event: dict) -> str:
    """Return a compact event type label for dedupe audit rows."""
    event_type = event.get("type") or "unknown"
    subtype = event.get("subtype")
    if subtype:
        return f"{event_type}.{subtype}"
    return event_type


def _mapping_get(mapping, key: str):
    """Get a key from dict-like Slack/Bolt values without assuming type."""
    getter = getattr(mapping, "get", None)
    if callable(getter):
        return getter(key)
    return None


def _slack_event_id(event: dict, context, body) -> str | None:
    """Extract Slack's event_id from the body/context shapes Bolt may provide."""
    return (
        _mapping_get(body, "event_id")
        or _mapping_get(context, "event_id")
        or _mapping_get(_mapping_get(context, "body"), "event_id")
        or _mapping_get(event, "event_id")
    )


async def _claim_slack_event(event: dict, context, body) -> str | None:
    """
    Atomically claim the Slack event_id for processing.

    Returns:
        - event_id when claimed and state should be tracked
        - "" when no event_id is available and processing should continue
        - None when this is a duplicate processing/completed event
    """
    event_id = _slack_event_id(event, context, body)
    if not event_id:
        return ""

    from pm_agent.app.db.message_queries import claim_event_processing
    from pm_agent.app.db.session import get_session_factory

    session_factory = get_session_factory()
    try:
        async with session_factory() as db:
            claimed = await claim_event_processing(db, event_id, _event_type_label(event))
            await db.commit()
            if not claimed:
                logger.info(f"Skipping duplicate Slack event_id={event_id}")
                return None
            return event_id
    except Exception as e:
        logger.error(f"Could not claim Slack event_id={event_id}: {e}", exc_info=True)
        return ""


async def _mark_slack_event_completed(event_id: str | None) -> None:
    """Mark a claimed event completed; no-op for untracked events."""
    if not event_id:
        return
    from pm_agent.app.db.message_queries import mark_event_completed
    from pm_agent.app.db.session import get_session_factory

    session_factory = get_session_factory()
    try:
        async with session_factory() as db:
            await mark_event_completed(db, event_id)
            await db.commit()
    except Exception as e:
        logger.error(f"Could not mark Slack event completed event_id={event_id}: {e}", exc_info=True)


async def _mark_slack_event_failed(event_id: str | None) -> None:
    """Mark a claimed event failed so Slack retries may process it again."""
    if not event_id:
        return
    from pm_agent.app.db.message_queries import mark_event_failed
    from pm_agent.app.db.session import get_session_factory

    session_factory = get_session_factory()
    try:
        async with session_factory() as db:
            await mark_event_failed(db, event_id)
            await db.commit()
    except Exception as e:
        logger.error(f"Could not mark Slack event failed event_id={event_id}: {e}", exc_info=True)


def register(app: AsyncApp) -> None:
    """Register Slack event handlers on the Bolt app."""

    @app.event("message")
    async def handle_message_event(event, say, context, body=None):
        """
        Handle incoming Slack messages.
        Saves raw messages from configured project channels.
        Does NOT run LLM extraction on every message.
        """
        from pm_agent.app.services.message_ingest_service import ingest_message

        event_id = await _claim_slack_event(event, context, body)
        if event_id is None:
            return
        try:
            await ingest_message(event, context)
            await _mark_slack_event_completed(event_id)
        except Exception as e:
            await _mark_slack_event_failed(event_id)
            logger.error(f"Error ingesting message: {e}", exc_info=True)

    @app.event("app_mention")
    async def handle_app_mention(event, say, context, body=None):
        """
        Handle @agent mentions in internal channels.
        Routes to developer Q&A service.
        """
        from pm_agent.app.services.question_answer_service import (
            handle_developer_question,
            should_silence_app_mention_error,
        )

        event_id = await _claim_slack_event(event, context, body)
        if event_id is None:
            return
        try:
            await handle_developer_question(event, say, context)
            await _mark_slack_event_completed(event_id)
        except Exception as e:
            await _mark_slack_event_failed(event_id)
            logger.error(f"Error handling app mention: {e}", exc_info=True)
            try:
                if await should_silence_app_mention_error(event.get("channel")):
                    return
            except Exception as silence_error:
                logger.error(
                    f"Could not determine app mention channel safety: {silence_error}",
                    exc_info=True,
                )
                return
            await say(
                text="Sorry, I encountered an error processing your question. Please try again.",
                thread_ts=event.get("thread_ts") or event.get("ts"),
            )

    @app.event("file_shared")
    async def handle_file_shared(event, context, body=None, client=None):
        """Persist standalone Slack file shares and schedule document parsing."""
        from pm_agent.app.services.document_service import register_file_shared_event

        event_id = await _claim_slack_event(event, context, body)
        if event_id is None:
            return
        try:
            await register_file_shared_event(event, client=client)
            await _mark_slack_event_completed(event_id)
        except Exception as e:
            await _mark_slack_event_failed(event_id)
            logger.error(f"Error ingesting file_shared event: {e}", exc_info=True)

    # Catch-all for unhandled events to prevent Bolt warnings
    @app.event({"type": "message", "subtype": "message_changed"})
    async def handle_message_changed(event, context, body=None):
        from pm_agent.app.services.message_ingest_service import ingest_message

        event_id = await _claim_slack_event(event, context, body)
        if event_id is None:
            return
        try:
            await ingest_message(event, context)
            await _mark_slack_event_completed(event_id)
        except Exception as e:
            await _mark_slack_event_failed(event_id)
            logger.error(f"Error ingesting message change: {e}", exc_info=True)

    @app.event({"type": "message", "subtype": "message_deleted"})
    async def handle_message_deleted(event, context, body=None):
        from pm_agent.app.services.message_ingest_service import ingest_message

        event_id = await _claim_slack_event(event, context, body)
        if event_id is None:
            return
        try:
            await ingest_message(event, context)
            await _mark_slack_event_completed(event_id)
        except Exception as e:
            await _mark_slack_event_failed(event_id)
            logger.error(f"Error ingesting message delete: {e}", exc_info=True)
