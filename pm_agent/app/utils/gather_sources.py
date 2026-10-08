"""Helpers for validating LLM gather output source IDs."""

import logging

logger = logging.getLogger(__name__)


def sanitize_gather_output_sources(output, valid_source_ids: set[str]) -> None:
    """Drop invalid source IDs and reject extracted items with no valid source."""
    output.confirmed_requirements = _items_with_valid_sources(
        output.confirmed_requirements, valid_source_ids, "confirmed requirement"
    )
    output.suggested_tasks = _items_with_valid_sources(
        output.suggested_tasks, valid_source_ids, "task"
    )
    output.open_clarifications = _items_with_valid_sources(
        output.open_clarifications, valid_source_ids, "clarification"
    )
    output.client_decisions = _items_with_valid_sources(
        output.client_decisions, valid_source_ids, "decision"
    )
    output.scope_risks = _items_with_valid_sources(
        output.scope_risks, valid_source_ids, "scope risk"
    )
    output.unconfirmed_assumptions = _items_with_valid_sources(
        output.unconfirmed_assumptions, valid_source_ids, "assumption"
    )


def collect_output_source_ids(output) -> set[str]:
    """Collect all valid source IDs still present in a gather output."""
    source_ids = set()
    for collection in (
        output.confirmed_requirements,
        output.suggested_tasks,
        output.open_clarifications,
        output.client_decisions,
        output.scope_risks,
        output.unconfirmed_assumptions,
    ):
        for item in collection:
            source_ids.update(str(source_id) for source_id in item.source_message_ids)
    return source_ids


def _items_with_valid_sources(items: list, valid_source_ids: set[str], label: str) -> list:
    """Keep only extracted items with at least one transcript-backed source."""
    kept = []
    for item in items:
        source_ids = getattr(item, "source_message_ids", []) or []
        valid_ids = []
        seen = set()
        for source_id in source_ids:
            source_id = str(source_id)
            if source_id in valid_source_ids and source_id not in seen:
                valid_ids.append(source_id)
                seen.add(source_id)
        item.source_message_ids = valid_ids
        if valid_ids:
            kept.append(item)
        else:
            title = (
                getattr(item, "title", None)
                or getattr(item, "text", None)
                or getattr(item, "question", None)
                or getattr(item, "decision", None)
                or getattr(item, "risk", None)
                or getattr(item, "assumption", None)
                or "untitled"
            )
            logger.warning("Rejected %s without valid sources: %s", label, title)
    return kept
