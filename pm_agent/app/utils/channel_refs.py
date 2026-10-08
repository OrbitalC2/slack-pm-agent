"""Helpers for Slack channel references used in setup commands."""

import re


CHANNEL_ID_PREFIXES = ("C", "G")


def extract_channel_id(value: str | None) -> str | None:
    """
    Extract a Slack channel ID from a direct ID or a <#C123|name> mention.

    Returns None for #channel-name references because those require Slack API
    lookup against the workspace conversation list.
    """
    if not value:
        return None

    ref = value.strip()
    mention_match = re.fullmatch(r"<#([^>|]+)(?:\|[^>]+)?>", ref)
    if mention_match:
        return mention_match.group(1)

    if ref.startswith(CHANNEL_ID_PREFIXES):
        return ref

    return None


def extract_channel_name(value: str | None) -> str | None:
    """Return a channel name from a #channel-name reference."""
    if not value:
        return None

    ref = value.strip()
    if ref.startswith("#") and len(ref) > 1:
        return ref[1:]
    return None
