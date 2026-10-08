"""Slack identifier helpers."""

import re


def looks_like_slack_user_id(value: str | None) -> bool:
    """Detect Slack user IDs; free-text names should stay as text."""
    if not value:
        return False
    return re.fullmatch(r"[UW][A-Z0-9]+", value.strip()) is not None
