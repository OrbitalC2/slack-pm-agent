"""
Structured source reference helpers.
"""

from typing import Any


def slack_message_source_ref(message: Any) -> dict:
    """Build a source ref for a stored Slack message ORM object."""
    return {
        "type": "slack_message",
        "message_id": str(message.id),
        "channel_id": message.slack_channel_id,
        "slack_message_ts": message.slack_message_ts,
        "permalink": message.permalink,
    }


def huddle_run_source_ref(huddle_run_id: Any, submitted_by_slack_id: str | None = None) -> dict:
    """Build a source ref for manually submitted huddle notes."""
    ref = {
        "type": "huddle_run",
        "huddle_run_id": str(huddle_run_id),
    }
    if submitted_by_slack_id:
        ref["submitted_by_slack_id"] = submitted_by_slack_id
    return ref


def document_source_refs(document: Any) -> list[dict]:
    """Build stable document and Slack-file provenance references."""
    return [
        {"type": "document", "id": str(document.id)},
        {"type": "slack_file", "id": document.slack_file_id},
    ]


def source_refs_for_message_ids(
    source_message_ids: list[str],
    source_refs_by_message_id: dict[str, dict],
) -> list[dict]:
    """Return source refs matching source message IDs, preserving item order."""
    refs = []
    seen = set()
    for message_id in source_message_ids:
        ref = source_refs_by_message_id.get(str(message_id))
        if not ref:
            continue
        key = (ref.get("type"), ref.get("message_id"), ref.get("huddle_run_id"))
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs
