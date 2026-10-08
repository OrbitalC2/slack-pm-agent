"""Tests for developer-ready task extraction (schemas, prompts, rendering)."""

from pathlib import Path

from pm_agent.app.agents.schemas import OpenClarification, SuggestedTask
from pm_agent.app.utils.slack_formatting import suggestion_blocks


def _section_texts(blocks):
    for block in blocks:
        if block.get("type") == "section":
            yield block["text"]["text"]


def test_suggested_task_schema_has_developer_ready_fields():
    task = SuggestedTask(title="Build search endpoint")
    assert task.acceptance_criteria == []
    assert task.dependencies == []
    assert task.blocked_by == []
    assert task.context == ""
    assert task.confidence == "medium"


def test_open_clarification_schema_is_a_decision_request():
    clar = OpenClarification(question="Which currencies?")
    assert clar.why_it_matters == ""
    assert clar.known_context == ""
    assert clar.options == []
    assert clar.recommended_option == ""
    assert clar.answer_owner == "client"
    assert clar.confidence == "medium"


def test_gather_prompts_require_developer_ready_tasks_and_decision_requests():
    client_prompt = Path(
        "pm_agent/app/agents/prompts/gather_client_info.md"
    ).read_text()
    internal_prompt = Path(
        "pm_agent/app/agents/prompts/gather_internal_info.md"
    ).read_text()

    for prompt in (client_prompt, internal_prompt):
        assert "developer-ready" in prompt.lower()
        assert "acceptance_criteria" in prompt
        assert "decision request" in prompt.lower()
        # Multiple concrete tasks, not one broad task.
        assert "MULTIPLE" in prompt
        # Structured clarification fields.
        assert "recommended_option" in prompt
        assert "answer_owner" in prompt


def test_task_suggestion_blocks_render_acceptance_criteria_and_dependencies():
    blocks = suggestion_blocks(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "suggestion_type": "task",
            "title": "Build search endpoint",
            "body": "Implement GET /search",
            "source_message_ids": ["m1"],
            "source_refs": [],
            "payload": {
                "priority": "high",
                "confidence": "high",
                "context": "Admins need to find customers fast",
                "acceptance_criteria": ["Returns 200", "Paginates results"],
                "dependencies": ["Auth service"],
                "blocked_by": [],
            },
            "gather_run_id": "00000000-0000-0000-0000-000000000002",
        }
    )

    text = "\n".join(_section_texts(blocks))
    assert "Acceptance criteria" in text
    assert "Returns 200" in text
    assert "Paginates results" in text
    assert "Context" in text

    actions = blocks[-2]["elements"]
    assert any(a["action_id"] == "approve_suggestion_task" for a in actions)


def test_task_approved_blocks_include_acceptance_criteria():
    from pm_agent.app.utils.slack_formatting import task_approved_blocks

    blocks = task_approved_blocks(
        {
            "title": "Build search endpoint",
            "description": "Implement GET /search",
            "owner_slack_id": "U_DEV",
            "priority": "high",
            "payload": {
                "context": "Admins need fast lookup",
                "acceptance_criteria": ["Returns 200", "Paginates results"],
                "dependencies": ["Auth service"],
            },
        },
        "Client Portal",
    )
    text = "\n".join(_section_texts(blocks))
    context_text = " ".join(
        el["text"]
        for block in blocks
        if block.get("type") == "context"
        for el in block.get("elements", [])
    )
    combined = text + " " + context_text
    assert "Acceptance criteria" in combined
    assert "Returns 200" in combined
    assert "Context" in combined
    assert "Dependencies" in combined
    assert combined.count("Implement GET /search") == 1


def test_gather_chain_accepts_multiple_concrete_tasks_with_acceptance_criteria():
    import asyncio

    from pm_agent.app.agents.chains import gather_info_chain

    class DummyLLM:
        async def generate_json(self, system_prompt, user_prompt, schema=None):
            return {
                "project_scope_summary": "Customer portal demo",
                "confirmed_requirements": [],
                "suggested_tasks": [
                    {
                        "title": "Build admin customer email search",
                        "description": "Search by exact or partial email",
                        "context": "Admin management screen",
                        "acceptance_criteria": [
                            "Admin can search by exact or partial email",
                            "Empty result state is shown",
                        ],
                        "dependencies": [],
                        "blocked_by": [],
                        "priority": "high",
                        "confidence": "high",
                        "source_message_ids": ["msg1"],
                    },
                    {
                        "title": "Add audit log for plan updates",
                        "description": "Record admin plan changes",
                        "acceptance_criteria": ["Each plan update writes an audit row"],
                        "source_message_ids": ["msg2"],
                    },
                ],
                "open_clarifications": [
                    {
                        "question": "Should cancelled users download old invoices?",
                        "why_it_matters": "Dashboard permission logic depends on it",
                        "options": ["Yes", "No"],
                        "recommended_option": "Yes",
                        "answer_owner": "client",
                        "source_message_ids": ["msg3"],
                    }
                ],
                "client_decisions": [],
                "scope_risks": [],
                "unconfirmed_assumptions": [],
            }

    output = asyncio.run(gather_info_chain("transcript", DummyLLM()))
    assert len(output.suggested_tasks) == 2
    assert output.suggested_tasks[0].acceptance_criteria
    assert len(output.open_clarifications) == 1
    assert output.open_clarifications[0].options


def test_task_blocks_handle_missing_payload_gracefully():
    # Huddle-created task suggestions may not include the new payload fields.
    blocks = suggestion_blocks(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "suggestion_type": "task",
            "title": "Legacy task",
            "body": "Do the thing",
            "source_message_ids": [],
            "gather_run_id": "",
        }
    )
    assert all(text.strip() for text in _section_texts(blocks))
