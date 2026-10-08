"""
Slack service — wrapper around Slack SDK for common operations.

Keeps Slack API calls centralized so service modules don't
directly depend on Slack SDK details.
"""

from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.errors import SlackApiError

from pm_agent.app.config import get_settings
from pm_agent.app.logging_config import get_logger

logger = get_logger("slack_service")


def get_slack_client() -> AsyncWebClient:
    """Get a Slack web client instance."""
    settings = get_settings()
    return AsyncWebClient(token=settings.slack_bot_token)


async def send_dm(client: AsyncWebClient, user_id: str, text: str = "", blocks: list | None = None) -> dict:
    """Send a direct message to a user."""
    # Open DM conversation
    dm = await client.conversations_open(users=[user_id])
    channel_id = dm["channel"]["id"]

    response = await client.chat_postMessage(
        channel=channel_id,
        text=text,
        blocks=blocks,
    )
    return response.data


async def send_channel_message(
    client: AsyncWebClient,
    channel_id: str,
    text: str = "",
    blocks: list | None = None,
    thread_ts: str | None = None,
) -> dict:
    """Send a message to a channel, optionally in a thread."""
    response = await client.chat_postMessage(
        channel=channel_id,
        text=text,
        blocks=blocks,
        thread_ts=thread_ts,
    )
    return response.data


async def update_message(
    client: AsyncWebClient,
    channel_id: str,
    message_ts: str,
    text: str = "",
    blocks: list | None = None,
) -> dict:
    """Update an existing Slack message."""
    response = await client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text=text,
        blocks=blocks,
    )
    return response.data


async def send_ephemeral(
    client: AsyncWebClient,
    channel_id: str,
    user_id: str,
    text: str,
) -> dict:
    """Send an ephemeral (private) message visible only to one user."""
    response = await client.chat_postEphemeral(
        channel=channel_id,
        user=user_id,
        text=text,
    )
    return response.data


async def open_modal(
    client: AsyncWebClient,
    trigger_id: str,
    view: dict,
) -> dict:
    """Open a Slack modal."""
    response = await client.views_open(
        trigger_id=trigger_id,
        view=view,
    )
    return response.data


async def get_permalink(
    client: AsyncWebClient,
    channel_id: str,
    message_ts: str,
) -> str | None:
    """Get a permalink for a Slack message. Returns None on failure."""
    try:
        response = await client.chat_getPermalink(
            channel=channel_id,
            message_ts=message_ts,
        )
        return response.get("permalink")
    except Exception as e:
        logger.warning(f"Failed to get permalink: {e}")
        return None


async def get_file_info(client: AsyncWebClient, file_id: str) -> dict:
    """Fetch the current Slack file object via files.info."""
    response = await client.files_info(file=file_id)
    return dict(response.get("file") or {})


async def create_channel_canvas(
    client: AsyncWebClient,
    channel_id: str,
    title: str,
    markdown: str,
) -> tuple[str, bool]:
    """Create a channel Canvas, or adopt the Canvas already attached there."""
    document_content = {"type": "markdown", "markdown": markdown}
    try:
        response = await client.conversations_canvases_create(
            channel_id=channel_id,
            title=title,
            document_content=document_content,
        )
    except AttributeError:
        response = await client.canvases_create(
            title=title,
            channel_id=channel_id,
            document_content=document_content,
        )
    except SlackApiError as exc:
        error = exc.response.get("error")
        if error == "channel_canvas_already_exists":
            canvas_id = await get_channel_canvas_id(client, channel_id)
            if canvas_id:
                return canvas_id, True
        if error in {"deprecated_endpoint", "method_deprecated"}:
            response = await client.canvases_create(
                title=title,
                channel_id=channel_id,
                document_content=document_content,
            )
        else:
            raise

    canvas_id = response.get("canvas_id")
    if not canvas_id:
        raise RuntimeError("Slack Canvas creation returned no canvas_id.")
    return str(canvas_id), False


async def get_channel_canvas_id(
    client: AsyncWebClient,
    channel_id: str,
) -> str | None:
    """Find the channel Canvas ID from conversations.info metadata."""
    response = await client.conversations_info(channel=channel_id)
    channel = response.get("channel") or {}
    properties = channel.get("properties") or {}
    canvas = properties.get("canvas")
    if isinstance(canvas, str):
        return canvas
    if isinstance(canvas, dict):
        for key in ("canvas_id", "file_id", "id"):
            if canvas.get(key):
                return str(canvas[key])

    for tab in properties.get("tabs") or []:
        if tab.get("type") != "canvas":
            continue
        data = tab.get("data") or {}
        for value in (
            tab.get("canvas_id"),
            tab.get("file_id"),
            data.get("canvas_id"),
            data.get("file_id"),
        ):
            if value:
                return str(value)

    solutions = channel.get("channel_solutions") or {}
    canvas_ids = solutions.get("canvas_ids") or []
    return str(canvas_ids[0]) if canvas_ids else None


async def edit_canvas(
    client: AsyncWebClient,
    canvas_id: str,
    markdown: str,
) -> dict:
    """Replace all Canvas content with one Markdown document."""
    response = await client.canvases_edit(
        canvas_id=canvas_id,
        changes=[
            {
                "operation": "replace",
                "document_content": {"type": "markdown", "markdown": markdown},
            }
        ],
    )
    return dict(response.data) if hasattr(response, "data") else dict(response)


async def fetch_channel_history(
    client: AsyncWebClient,
    channel_id: str,
    oldest: str | None = None,
    latest: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Fetch channel message history using conversations.history.
    Handles pagination for large channels.
    """
    all_messages = []
    cursor = None

    while True:
        kwargs = {
            "channel": channel_id,
            "limit": min(limit - len(all_messages), 200),
        }
        if oldest:
            kwargs["oldest"] = oldest
        if latest:
            kwargs["latest"] = latest
        if cursor:
            kwargs["cursor"] = cursor

        response = await client.conversations_history(**kwargs)
        messages = response.get("messages", [])
        all_messages.extend(messages)

        # Check for pagination
        metadata = response.get("response_metadata", {})
        cursor = metadata.get("next_cursor")

        if not cursor or len(all_messages) >= limit:
            break

    return all_messages
