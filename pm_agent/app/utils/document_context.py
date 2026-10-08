"""Compact, Slack-prompt-safe formatting for linked document analysis."""

import json


MAX_CONTEXT_ITEMS_PER_TYPE = 10
MAX_CONTEXT_ITEM_CHARS = 400
MAX_DOCUMENT_SUMMARY_CHARS = 1200


def format_document_context(document, chunk_count: int) -> str:
    """Format metadata and structured analysis without full extracted text."""
    name = document.title or document.filename or document.slack_file_id
    lines = [f"[Document uploaded: {name}]", f"Document ID: {document.id}"]
    lines.append(_status_line("Download", document.download_status, document.download_error))
    lines.append(_status_line("Parse", document.parse_status, document.parse_error))
    lines.append(_status_line("Analysis", document.analysis_status, None))
    if document.page_count is not None:
        lines.append(f"Pages: {document.page_count}")
    lines.append(f"Chunks: {chunk_count}")

    if document.analysis_summary:
        lines.extend(
            ["Summary:", _truncate(document.analysis_summary, MAX_DOCUMENT_SUMMARY_CHARS)]
        )

    items = document.analysis_items or {}
    specs = (
        ("requirements", "Extracted requirements", "text"),
        ("suggested_tasks", "Suggested tasks", "title"),
        ("clarifications", "Open questions", "question"),
        ("decisions", "Decisions", "text"),
        ("risks", "Risks", "text"),
        ("assumptions", "Assumptions", "text"),
        ("features", "Features/modules", "name"),
    )
    for key, label, primary_field in specs:
        values = list(items.get(key) or [])[:MAX_CONTEXT_ITEMS_PER_TYPE]
        if not values:
            continue
        lines.append(f"{label}:")
        for value in values:
            primary = value.get(primary_field) if isinstance(value, dict) else str(value)
            if primary:
                lines.append(f"- {_truncate(str(primary), MAX_CONTEXT_ITEM_CHARS)}")

    lines.append(
        "Source ref: "
        + json.dumps({"type": "document", "id": str(document.id)}, separators=(",", ":"))
    )
    return "\n".join(lines)


def _status_line(label: str, status: str, error: str | None) -> str:
    line = f"{label} status: {status}"
    if error:
        line += f" ({_truncate(error, MAX_CONTEXT_ITEM_CHARS)})"
    return line


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
