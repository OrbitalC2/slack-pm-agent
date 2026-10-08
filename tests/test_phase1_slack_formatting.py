from pm_agent.app.utils.slack_formatting import (
    MAX_BLOCKS_PER_MESSAGE,
    MAX_MODAL_BODY_INITIAL,
    MAX_MODAL_TITLE_INITIAL,
    edit_suggestion_modal,
    gather_summary_block_batches,
    huddle_summary_block_batches,
    suggestion_blocks,
    text_block,
)


def _section_texts(blocks):
    for block in blocks:
        if block.get("type") == "section":
            yield block["text"]["text"]


def test_text_block_never_emits_empty_text():
    block = text_block("")

    assert block["text"]["text"]


def test_decision_with_empty_body_does_not_emit_empty_section():
    blocks = suggestion_blocks(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "suggestion_type": "decision",
            "title": "Use Stripe",
            "body": "",
            "source_message_ids": [],
            "gather_run_id": "00000000-0000-0000-0000-000000000002",
        }
    )

    assert all(text.strip() for text in _section_texts(blocks))


def test_scope_risk_and_assumption_have_acknowledge_buttons():
    risk_blocks = suggestion_blocks(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "suggestion_type": "scope_risk",
            "title": "Unclear export format",
            "body": "CSV vs XLSX is unresolved",
            "source_message_ids": [],
            "gather_run_id": "00000000-0000-0000-0000-000000000002",
        }
    )
    assumption_blocks = suggestion_blocks(
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "suggestion_type": "assumption",
            "title": "Admin users can export",
            "body": "Needs confirmation from: client",
            "source_message_ids": [],
            "gather_run_id": "00000000-0000-0000-0000-000000000002",
        }
    )

    risk_actions = risk_blocks[-2]["elements"]
    assumption_actions = assumption_blocks[-2]["elements"]
    assert any(a["action_id"] == "acknowledge_scope_risk" for a in risk_actions)
    assert any(a["action_id"] == "acknowledge_assumption" for a in assumption_actions)


def test_gather_summary_does_not_duplicate_section_headings():
    suggestions = [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "suggestion_type": "task",
            "title": "Task A",
            "body": "Do A",
            "source_message_ids": [],
            "gather_run_id": "00000000-0000-0000-0000-000000000999",
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "suggestion_type": "task",
            "title": "Task B",
            "body": "Do B",
            "source_message_ids": [],
            "gather_run_id": "00000000-0000-0000-0000-000000000999",
        },
        {
            "id": "00000000-0000-0000-0000-000000000003",
            "suggestion_type": "scope_risk",
            "title": "Risk A",
            "body": "Might slip",
            "source_message_ids": [],
            "gather_run_id": "00000000-0000-0000-0000-000000000999",
        },
    ]

    batches = gather_summary_block_batches(
        "Project",
        "Summary",
        [],
        suggestions,
        "00000000-0000-0000-0000-000000000999",
    )

    headers = [
        block["text"]["text"]
        for batch in batches
        for block in batch
        if block.get("type") == "header"
    ]
    assert headers.count("📋 Suggested Developer-Ready Tasks") == 1
    assert headers.count("⚠️ Implementation Risks / Blockers") == 1


def test_gather_summary_batches_stay_under_block_limit():
    suggestions = [
        {
            "id": f"00000000-0000-0000-0000-{idx:012d}",
            "suggestion_type": "task",
            "title": f"Task {idx}",
            "body": "Do the thing",
            "source_message_ids": ["m1"],
            "gather_run_id": "00000000-0000-0000-0000-000000000999",
        }
        for idx in range(20)
    ]

    batches = gather_summary_block_batches(
        "Project",
        "Summary",
        [{"text": "Requirement"}],
        suggestions,
        "00000000-0000-0000-0000-000000000999",
    )

    assert len(batches) > 1
    assert all(0 < len(batch) <= MAX_BLOCKS_PER_MESSAGE for batch in batches)


def test_huddle_summary_batches_stay_under_block_limit():
    suggestions = [
        {
            "id": f"00000000-0000-0000-0000-{idx:012d}",
            "suggestion_type": "task",
            "title": f"Huddle task {idx}",
            "body": "Do the thing",
            "source_message_ids": [],
            "source_refs": [{"type": "huddle_run", "huddle_run_id": "h1"}],
            "gather_run_id": "",
        }
        for idx in range(30)
    ]

    batches = huddle_summary_block_batches("Project", "Summary", suggestions)

    assert len(batches) > 1
    assert all(0 < len(batch) <= MAX_BLOCKS_PER_MESSAGE for batch in batches)


def test_edit_modal_saves_edits_without_approving():
    modal = edit_suggestion_modal(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "title": "Task",
            "body": "Details",
            "payload": {"priority": "medium"},
        }
    )

    assert modal["submit"]["text"] == "Save Edits"


def test_edit_modal_initial_values_are_truncated_for_slack_limits():
    long_title = "T" * (MAX_MODAL_TITLE_INITIAL + 50)
    long_body = "B" * (MAX_MODAL_BODY_INITIAL + 50)

    modal = edit_suggestion_modal(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "title": long_title,
            "body": long_body,
            "payload": {"priority": "medium"},
        }
    )

    title_value = modal["blocks"][0]["element"]["initial_value"]
    body_value = modal["blocks"][1]["element"]["initial_value"]

    assert len(title_value) == MAX_MODAL_TITLE_INITIAL
    assert len(body_value) == MAX_MODAL_BODY_INITIAL
    assert title_value.endswith("…")
    assert body_value.endswith("…")


def test_huddle_summary_tiny_batches_never_exceed_limit():
    suggestions = [
        {
            "id": f"00000000-0000-0000-0000-{idx:012d}",
            "suggestion_type": "task",
            "title": f"Huddle task {idx}",
            "body": "Do the thing",
            "source_message_ids": [],
            "source_refs": [{"type": "huddle_run", "huddle_run_id": "h1"}],
            "gather_run_id": "",
        }
        for idx in range(3)
    ]

    batches = huddle_summary_block_batches("Project", "Summary", suggestions, max_blocks=5)

    assert len(batches) > 1
    assert all(0 < len(batch) <= 5 for batch in batches)
