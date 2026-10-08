"""
Text cleaning and normalization utilities for Slack messages.
"""

import re
from typing import Any


def normalize_slack_text(raw: str) -> str:
    """
    Normalize Slack message text for cleaner retrieval.

    - Converts <@U123> user mentions to @user
    - Converts <#C123|channel-name> channel mentions to #channel-name
    - Converts <URL|label> links to label (URL)
    - Strips Slack-specific emoji shortcodes like :thumbsup:
    - Strips excessive whitespace
    """
    if not raw:
        return ""

    text = raw

    # Convert user mentions <@U123> -> @user
    text = re.sub(r"<@(U[A-Z0-9]+)>", r"@\1", text)

    # Convert channel mentions <#C123|channel-name> -> #channel-name
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)

    # Convert links <URL|label> -> label
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)

    # Convert bare links <URL> -> URL
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)

    # Strip custom emoji :emoji_name:
    text = re.sub(r":([a-zA-Z0-9_+-]+):", r"", text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def slack_file_placeholders(event: dict[str, Any]) -> str:
    """Build lightweight text placeholders for Slack file shares."""
    placeholders = []
    for file_info in event.get("files") or []:
        title = file_info.get("title") or file_info.get("name") or "untitled file"
        filetype = file_info.get("filetype") or file_info.get("mimetype") or "file"
        url = file_info.get("permalink") or file_info.get("url_private") or ""
        url_part = f" - {url}" if url else ""
        placeholders.append(f"[File shared: {title} ({filetype}){url_part}]")
    return "\n".join(placeholders)


def message_text_with_file_placeholders(event: dict[str, Any]) -> str:
    """Combine Slack message text with file placeholders, if any."""
    text = event.get("text") or ""
    placeholders = slack_file_placeholders(event)
    if text and placeholders:
        return f"{text}\n{placeholders}"
    return text or placeholders


def format_transcript(
    messages: list[dict[str, Any]],
    include_ids: bool = True,
) -> str:
    """
    Format a list of messages as a labeled transcript for LLM processing.

    Each message dict should have:
        - id: UUID string (DB message ID)
        - sender_type: project_manager/client/developer/bot/unknown
        - sender_slack_id: Slack user ID
        - raw_text: original message text
        - slack_message_ts: Slack timestamp
        - slack_thread_ts: optional thread timestamp

    Returns a formatted transcript string with speaker labels and timestamps.
    """
    lines = []

    for msg in messages:
        sender = msg.get("sender_type", "unknown")
        sender_id = msg.get("sender_slack_id", "?")
        text = msg.get("normalized_text") or msg.get("raw_text", "")
        ts = msg.get("slack_message_ts", "?")
        msg_id = msg.get("id", "?")
        thread = msg.get("slack_thread_ts")

        label = f"[{sender}:{sender_id}]"
        ts_label = f"[ts:{ts}]"
        id_label = f"[msg_id:{msg_id}]" if include_ids else ""
        thread_label = f"[thread:{thread}]" if thread else ""

        line_parts = [label, ts_label, id_label, thread_label, text]
        line = " ".join(part for part in line_parts if part)
        lines.append(line)

    return "\n".join(lines)
