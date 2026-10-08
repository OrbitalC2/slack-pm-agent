"""
Slack Block Kit formatting utilities.

Builds Block Kit blocks for:
- Gather-info summary with approval buttons
- Task notifications for internal channel
- Status/update/blocker reports
- Modals for setup, edit, huddle notes
"""

import json
from typing import Any

MAX_SECTION_TEXT = 3000
MAX_CONTEXT_TEXT = 3000
MAX_HEADER_TEXT = 150
MAX_BUTTON_TEXT = 75
MAX_BLOCKS_PER_MESSAGE = 40
MAX_MODAL_TITLE_INITIAL = 150
MAX_MODAL_BODY_INITIAL = 2900
FALLBACK_SECTION_TEXT = "_No additional details._"
MAX_CLARIFICATION_OPTION_BUTTONS = 3


# ── Block Builders ──


def text_block(text: str, block_type: str = "mrkdwn") -> dict:
    """Create a simple text section block."""
    safe_text = _safe_text(text, MAX_SECTION_TEXT, FALLBACK_SECTION_TEXT)
    return {
        "type": "section",
        "text": {"type": block_type, "text": safe_text},
    }


def divider_block() -> dict:
    """Create a divider block."""
    return {"type": "divider"}


def header_block(text: str) -> dict:
    """Create a header block."""
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": _safe_text(text, MAX_HEADER_TEXT, "Untitled")},
    }


def context_block(text: str) -> dict:
    """Create a context block with mrkdwn text."""
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": _safe_text(text, MAX_CONTEXT_TEXT, FALLBACK_SECTION_TEXT)}],
    }


# ── Suggestion Approval Blocks ──


def suggestion_blocks(suggestion: dict) -> list[dict]:
    """
    Build Block Kit blocks for a single suggestion with approval buttons.

    suggestion dict should have: id, suggestion_type, title, body,
    source_message_ids, source_refs, payload, gather_run_id
    """
    suggestion_id = str(suggestion["id"])
    gather_run_id = str(suggestion.get("gather_run_id", ""))
    stype = suggestion["suggestion_type"]
    title = _safe_text(suggestion.get("title", "Untitled"), 250, "Untitled")
    body = (suggestion.get("body") or "").strip()
    payload = suggestion.get("payload") or {}

    type_emoji = {
        "task": "📋",
        "clarification": "❓",
        "decision": "✅",
        "scope_risk": "⚠️",
        "assumption": "💭",
    }
    emoji = type_emoji.get(stype, "📌")

    blocks = [text_block(f"{emoji} *{stype.replace('_', ' ').title()}*: {title}")]

    if stype == "task":
        blocks.extend(_task_detail_blocks(body, payload))
    elif stype == "clarification":
        blocks.extend(_clarification_detail_blocks(body, payload))
    elif body:
        blocks.append(text_block(body))

    # Source context — prefer a clickable permalink, else a reference count.
    blocks.append(context_block(_source_context_text(suggestion)))

    # Action buttons based on type
    value_payload = json.dumps({
        "suggestion_id": suggestion_id,
        "gather_run_id": gather_run_id,
    })

    buttons = []

    if stype == "task":
        buttons.append(_button("Approve Task", "approve_suggestion_task", value_payload, "primary"))
        buttons.append(_button("Edit", "edit_suggestion", value_payload))
        buttons.append(_button("Reject", "reject_suggestion", value_payload, "danger"))
    elif stype == "clarification":
        # Decision-request actions: answer with option A/B/C, edit, or ask the client.
        options = [o for o in (payload.get("options") or []) if str(o).strip()]
        for idx, _option in enumerate(options[:MAX_CLARIFICATION_OPTION_BUTTONS]):
            option_payload = json.dumps({
                "suggestion_id": suggestion_id,
                "gather_run_id": gather_run_id,
                "option_index": idx,
            })
            buttons.append(_button(
                chr(65 + idx),
                f"answer_clarification_option_{idx}",
                option_payload,
                "primary",
            ))
        if not options:
            buttons.append(_button("Answer", "answer_clarification_internally", value_payload, "primary"))
        buttons.append(_button("Edit", "edit_suggestion", value_payload))
        buttons.append(_button("Ask Client", "approve_suggestion_clarification", value_payload))
    else:
        if stype == "decision":
            buttons.append(_button("Approve Decision", "approve_suggestion_decision", value_payload, "primary"))
        elif stype == "scope_risk":
            buttons.append(_button("Acknowledge Risk", "acknowledge_scope_risk", value_payload, "primary"))
        elif stype == "assumption":
            buttons.append(_button("Acknowledge Assumption", "acknowledge_assumption", value_payload, "primary"))
        buttons.append(_button("Edit", "edit_suggestion", value_payload))
        buttons.append(_button("Reject", "reject_suggestion", value_payload, "danger"))

    blocks.append({
        "type": "actions",
        "block_id": f"suggestion_actions_{suggestion_id}",
        "elements": buttons,
    })
    blocks.append(divider_block())

    return blocks


def _task_detail_blocks(body: str, payload: dict) -> list[dict]:
    """Render developer-ready task detail (context, acceptance criteria, deps)."""
    blocks = []
    if body:
        blocks.append(text_block(body))

    context = (payload.get("context") or "").strip()
    if context:
        blocks.append(text_block(f"*Context:* {context}"))

    criteria = [c for c in (payload.get("acceptance_criteria") or []) if str(c).strip()]
    if criteria:
        bullet_text = "*Acceptance criteria:*\n" + "\n".join(f"• {c}" for c in criteria)
        blocks.append(text_block(bullet_text))

    meta = _join_inline_lists(payload, [("dependencies", "Dependencies"), ("blocked_by", "Blocked by")])
    priority = payload.get("priority")
    confidence = payload.get("confidence")
    tags = []
    if priority:
        tags.append(f"Priority: {priority}")
    if confidence:
        tags.append(f"Confidence: {confidence}")
    if tags:
        meta = (meta + "\n" if meta else "") + " · ".join(tags)
    if meta:
        blocks.append(context_block(meta))
    return blocks


def _clarification_detail_blocks(body: str, payload: dict) -> list[dict]:
    """Render a structured decision request for a clarification."""
    blocks = []
    why = (payload.get("why_it_matters") or body or "").strip()
    if why:
        blocks.append(text_block(f"*Why it matters:* {why}"))

    known = (payload.get("known_context") or "").strip()
    if known:
        blocks.append(text_block(f"*Known context:* {known}"))

    options = [o for o in (payload.get("options") or []) if str(o).strip()]
    if options:
        labelled = "\n".join(
            f"{chr(65 + i)}. {opt}" for i, opt in enumerate(options)
        )
        blocks.append(text_block(f"*Options:*\n{labelled}"))

    recommended = (payload.get("recommended_option") or "").strip()
    if recommended:
        blocks.append(text_block(f"*Recommended:* {recommended}"))

    owner = (payload.get("answer_owner") or "").strip()
    confidence = (payload.get("confidence") or "").strip()
    tags = []
    if owner:
        tags.append(f"Needs answer from: {owner}")
    if confidence:
        tags.append(f"Confidence: {confidence}")
    if tags:
        blocks.append(context_block(" · ".join(tags)))
    return blocks


def _join_inline_lists(payload: dict, specs: list[tuple[str, str]]) -> str:
    """Build a compact context line from list-valued payload fields."""
    parts = []
    for key, label in specs:
        values = [str(v).strip() for v in (payload.get(key) or []) if str(v).strip()]
        if values:
            parts.append(f"{label}: {', '.join(values)}")
    return "\n".join(parts)


def _source_context_text(suggestion: dict) -> str:
    """Return a source line with a permalink when available, else a count."""
    source_refs = suggestion.get("source_refs") or []
    for ref in source_refs:
        permalink = ref.get("permalink") if isinstance(ref, dict) else None
        if permalink:
            return f"📎 <{permalink}|Source>"
    source_ids = suggestion.get("source_message_ids") or []
    if source_ids:
        return f"📎 Source messages: {len(source_ids)} referenced"
    if source_refs:
        return f"📎 Source references: {len(source_refs)} referenced"
    return "📎 No linked sources"


def gather_summary_block_batches(
    project_name: str,
    scope_summary: str,
    requirements: list[dict],
    suggestions: list[dict],
    gather_run_id: str,
    documents: list[str] | None = None,
    max_blocks: int = MAX_BLOCKS_PER_MESSAGE,
) -> list[list[dict]]:
    """
    Build Slack-safe, grouped gather-info messages split below the block limit.

    Sections: scope summary, confirmed requirements, suggested developer-ready
    tasks, decision requests/clarifications, implementation risks/blockers,
    documents considered. Sources are linked inline on each item.
    """
    suggestions = _dedupe_suggestions(suggestions)

    blocks = [
        header_block(f"📊 Gather Info: {project_name}"),
        text_block(f"*Scope summary:*\n{scope_summary or 'No summary produced.'}"),
        divider_block(),
    ]

    # Confirmed requirements
    if requirements:
        req_text = "*Confirmed requirements:*\n"
        for i, req in enumerate(requirements, 1):
            req_text += f"{i}. {req.get('text', '')}\n"
        blocks.append(text_block(req_text))
        blocks.append(divider_block())

    # Documents considered
    if documents:
        doc_text = "*Documents considered:*\n" + "\n".join(
            f"• {name}" for name in documents
        )
        blocks.append(context_block(doc_text))

    task_suggestions = [s for s in suggestions if s["suggestion_type"] == "task"]

    batches = [_trim_blocks(blocks, max_blocks)]
    current: list[dict] = []

    # Group suggestions by type with clear, non-duplicated section headings.
    type_order = ["task", "clarification", "decision", "scope_risk", "assumption"]
    type_labels = {
        "task": "📋 Suggested Developer-Ready Tasks",
        "clarification": "❓ Decision Requests / Clarifications",
        "decision": "✅ Confirmed Decisions",
        "scope_risk": "⚠️ Implementation Risks / Blockers",
        "assumption": "💭 Unconfirmed Assumptions",
    }

    for stype in type_order:
        typed = [s for s in suggestions if s["suggestion_type"] == stype]
        if typed:
            type_header = header_block(type_labels.get(stype, stype))
            current = _append_block_group(batches, current, [type_header], max_blocks)
            if stype == "task" and len(task_suggestions) > 1:
                current = _append_block_group(
                    batches,
                    current,
                    [_approve_all_tasks_block(len(task_suggestions), gather_run_id)],
                    max_blocks,
                )
            for s in typed:
                suggestion_group = suggestion_blocks(s)
                if len(current) + len(suggestion_group) > max_blocks and current:
                    batches.append(_trim_blocks(current, max_blocks))
                    current = [type_header]
                current = _append_block_group(batches, current, suggestion_group, max_blocks)

    if current:
        batches.append(_trim_blocks(current, max_blocks))

    return [batch for batch in batches if batch]


def _dedupe_suggestions(suggestions: list[dict]) -> list[dict]:
    """Drop duplicate suggestions sharing a type and normalized title."""
    seen = set()
    unique = []
    for s in suggestions:
        key = (s.get("suggestion_type"), str(s.get("title", "")).strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)
    return unique


def _approve_all_tasks_block(task_count: int, gather_run_id: str) -> dict:
    return {
        "type": "actions",
        "block_id": f"approve_all_tasks_{gather_run_id}",
        "elements": [
            _button(
                f"Approve All {task_count} Tasks",
                "approve_all_tasks_from_gather",
                json.dumps({"gather_run_id": gather_run_id}),
                "primary",
            )
        ],
    }


def huddle_summary_block_batches(
    project_name: str,
    summary: str,
    suggestions: list[dict],
    max_blocks: int = MAX_BLOCKS_PER_MESSAGE,
) -> list[list[dict]]:
    """Build Slack-safe huddle result messages split below the block limit."""
    blocks = [header_block(f"📝 Huddle Notes: {project_name}")]

    if summary:
        blocks.append(text_block(f"*Summary:*\n{summary}"))
        blocks.append(divider_block())

    if not suggestions:
        blocks.append(text_block("No specific tasks, decisions, or clarifications extracted from the notes."))
        return [_trim_blocks(blocks, max_blocks)]

    batches = [_trim_blocks(blocks, max_blocks)]
    current: list[dict] = []

    type_order = ["task", "clarification", "decision", "scope_risk", "assumption"]
    type_labels = {
        "task": "📋 Huddle Tasks",
        "clarification": "❓ Huddle Clarifications",
        "decision": "✅ Huddle Decisions",
        "scope_risk": "⚠️ Scope Risks",
        "assumption": "💭 Assumptions",
    }

    for stype in type_order:
        typed = [s for s in suggestions if s["suggestion_type"] == stype]
        if not typed:
            continue
        type_header = header_block(type_labels.get(stype, stype))
        current = _append_block_group(batches, current, [type_header], max_blocks)
        for suggestion in typed:
            suggestion_group = suggestion_blocks(suggestion)
            if len(current) + len(suggestion_group) > max_blocks and current:
                batches.append(_trim_blocks(current, max_blocks))
                current = [type_header]
            current = _append_block_group(batches, current, suggestion_group, max_blocks)

    if current:
        batches.append(_trim_blocks(current, max_blocks))

    return [batch for batch in batches if batch]


# ── Task Notification Blocks ──


def task_approved_blocks(task: dict, project_name: str) -> list[dict]:
    """
    Build blocks for posting an approved task in the internal channel.
    """
    title = task.get("title", "Untitled")
    description = task.get("description", "")
    owner = task.get("owner_slack_id")
    priority = task.get("priority", "medium")
    payload = task.get("payload") or {}

    priority_emoji = {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "urgent": "🔴",
    }

    blocks = [
        text_block(
            f"📋 *New task approved from client discussion*\n"
            f"*Title:* {title}\n"
            f"*Priority:* {priority_emoji.get(priority, '🟡')} {priority}\n"
            f"*Owner:* {f'<@{owner}>' if owner else 'Unassigned'}\n"
            f"*Project:* {project_name}"
        ),
    ]

    if description:
        blocks.append(text_block(f"*Description:*\n{description}"))

    blocks.extend(_task_detail_blocks("", payload))

    return blocks


# ── Modal Builders ──


def setup_project_modal(
    channel_id: str,
    default_client_channel_id: str | None = None,
) -> dict:
    """Build the project setup modal view."""
    client_channel_element = {
        "type": "conversations_select",
        "action_id": "client_channel_input",
        "placeholder": {"type": "plain_text", "text": "Select client channel"},
    }
    if default_client_channel_id:
        client_channel_element["initial_conversation"] = default_client_channel_id

    return {
        "type": "modal",
        "callback_id": "setup_project_modal",
        "title": {"type": "plain_text", "text": "Setup Project"},
        "submit": {"type": "plain_text", "text": "Save Project"},
        "private_metadata": json.dumps({"channel_id": channel_id}),
        "blocks": [
            {
                "type": "input",
                "block_id": "project_name",
                "label": {"type": "plain_text", "text": "Project Name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "project_name_input",
                    "placeholder": {"type": "plain_text", "text": "e.g., Invoice Dashboard"},
                },
            },
            {
                "type": "input",
                "block_id": "client_name",
                "optional": True,
                "label": {"type": "plain_text", "text": "Client Name"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "client_name_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g., Example Client",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "client_channel",
                "label": {"type": "plain_text", "text": "Client Channel"},
                "element": client_channel_element,
            },
            {
                "type": "input",
                "block_id": "internal_channel",
                "optional": True,
                "label": {"type": "plain_text", "text": "Internal Channel"},
                "element": {
                    "type": "conversations_select",
                    "action_id": "internal_channel_input",
                    "placeholder": {"type": "plain_text", "text": "Select internal channel"},
                },
            },
            {
                "type": "input",
                "block_id": "github_repo",
                "optional": True,
                "label": {"type": "plain_text", "text": "GitHub Repo URL"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "github_repo_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "https://github.com/example/repository",
                    },
                },
            },
        ],
    }


def huddle_notes_modal(channel_id: str) -> dict:
    """Build the huddle notes modal view."""
    return {
        "type": "modal",
        "callback_id": "huddle_notes_modal",
        "title": {"type": "plain_text", "text": "Huddle Notes"},
        "submit": {"type": "plain_text", "text": "Process Notes"},
        "private_metadata": json.dumps({"channel_id": channel_id}),
        "blocks": [
            {
                "type": "input",
                "block_id": "huddle_notes",
                "label": {"type": "plain_text", "text": "Huddle Notes"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "huddle_notes_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Paste rough notes from the huddle (Urdu/English/Roman Urdu accepted)...",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "participants",
                "optional": True,
                "label": {"type": "plain_text", "text": "Participants"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "participants_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Comma-separated names or @mentions",
                    },
                },
            },
        ],
    }


def edit_suggestion_modal(suggestion: dict) -> dict:
    """Build the edit suggestion modal view."""
    initial_title = _modal_initial_value(
        suggestion.get("title", ""),
        MAX_MODAL_TITLE_INITIAL,
    )
    initial_body = _modal_initial_value(
        suggestion.get("body", ""),
        MAX_MODAL_BODY_INITIAL,
    )
    return {
        "type": "modal",
        "callback_id": "edit_suggestion_modal",
        "title": {"type": "plain_text", "text": "Edit Suggestion"},
        "submit": {"type": "plain_text", "text": "Save Edits"},
        "private_metadata": json.dumps({"suggestion_id": str(suggestion["id"])}),
        "blocks": [
            {
                "type": "input",
                "block_id": "edit_title",
                "label": {"type": "plain_text", "text": "Title"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_title_input",
                    "initial_value": initial_title,
                },
            },
            {
                "type": "input",
                "block_id": "edit_body",
                "label": {"type": "plain_text", "text": "Description"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "edit_body_input",
                    "multiline": True,
                    "initial_value": initial_body,
                },
            },
            {
                "type": "input",
                "block_id": "edit_priority",
                "optional": True,
                "label": {"type": "plain_text", "text": "Priority"},
                "element": {
                    "type": "static_select",
                    "action_id": "edit_priority_input",
                    "options": [
                        {"text": {"type": "plain_text", "text": p}, "value": p}
                        for p in ["low", "medium", "high", "urgent"]
                    ],
                    "initial_option": {
                        "text": {
                            "type": "plain_text",
                            "text": suggestion.get("payload", {}).get("priority", "medium"),
                        },
                        "value": suggestion.get("payload", {}).get("priority", "medium"),
                    },
                },
            },
        ],
    }


def clarification_answer_modal(suggestion: dict) -> dict:
    """Build the modal for answering a clarification internally."""
    payload = suggestion.get("payload") or {}
    metadata = {"suggestion_id": str(suggestion["id"])}
    if suggestion.get("message_channel_id") and suggestion.get("message_ts"):
        metadata["message_channel_id"] = suggestion["message_channel_id"]
        metadata["message_ts"] = suggestion["message_ts"]
    initial_answer = _modal_initial_value(
        payload.get("recommended_option", ""),
        MAX_MODAL_BODY_INITIAL,
    )
    answer_element = {
        "type": "plain_text_input",
        "action_id": "clarification_answer_input",
        "multiline": True,
        "placeholder": {
            "type": "plain_text",
            "text": "Record the confirmed answer/decision for this clarification.",
        },
    }
    if initial_answer:
        answer_element["initial_value"] = initial_answer
    return {
        "type": "modal",
        "callback_id": "clarification_answer_modal",
        "title": {"type": "plain_text", "text": "Answer Clarification"},
        "submit": {"type": "plain_text", "text": "Save Answer"},
        "private_metadata": json.dumps(metadata),
        "blocks": [
            text_block(f"*Question:* {_safe_text(suggestion.get('title', ''), 250, 'Clarification')}"),
            {
                "type": "input",
                "block_id": "clarification_answer",
                "label": {"type": "plain_text", "text": "Confirmed answer"},
                "element": answer_element,
            },
        ],
    }


def decision_request_dm_blocks(project_name: str, suggestion: dict) -> list[dict]:
    """Build a single dev-raised decision request DM for admins."""
    blocks = [header_block(f"❓ Decision request: {project_name}")]
    submitter = (suggestion.get("payload") or {}).get("submitter_slack_id")
    if submitter:
        blocks.append(context_block(f"Raised by <@{submitter}>"))
    blocks.extend(suggestion_blocks(suggestion))
    return blocks


# ── Private Helpers ──


def _button(
    text: str,
    action_id: str,
    value: str,
    style: str | None = None,
) -> dict:
    """Create a Block Kit button element."""
    btn = {
        "type": "button",
        "text": {"type": "plain_text", "text": _safe_text(text, MAX_BUTTON_TEXT, "Action")},
        "action_id": action_id,
        "value": value,
    }
    if style:
        btn["style"] = style
    return btn


def _safe_text(text: Any, max_chars: int, fallback: str) -> str:
    """Return non-empty Slack-safe text."""
    safe = str(text or "").strip()
    if not safe:
        safe = fallback
    if len(safe) <= max_chars:
        return safe
    return safe[: max_chars - 1].rstrip() + "…"


def _modal_initial_value(text: Any, max_chars: int) -> str:
    """Return Slack-safe modal initial_value text without changing DB content."""
    safe = str(text or "")
    if len(safe) <= max_chars:
        return safe
    return safe[: max_chars - 1].rstrip() + "…"


def _append_block_group(
    batches: list[list[dict]],
    current: list[dict],
    group: list[dict],
    max_blocks: int,
) -> list[dict]:
    """Append a group to the current batch, flushing if needed."""
    if len(group) > max_blocks:
        if current:
            batches.append(_trim_blocks(current, max_blocks))
        batches.append(_trim_blocks(group, max_blocks))
        return []
    if len(current) + len(group) > max_blocks and current:
        batches.append(_trim_blocks(current, max_blocks))
        return list(group)
    current.extend(group)
    return current


def _trim_blocks(blocks: list[dict], max_blocks: int) -> list[dict]:
    """Defensive cap for block count."""
    return blocks[:max_blocks]
