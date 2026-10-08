"""Focused tests for the dev-safe operational Canvas rendering."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from pm_agent.app.services import canvas_service


NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


def _project(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Client Portal",
        "client_name": "Example Client",
        "client_channel_id": "C_CLIENT",
        "internal_channel_id": "C_INTERNAL",
        "github_repo_url": "https://github.com/example/portal",
        "project_summary": "Billing portal",
        "internal_canvas_id": None,
        "internal_canvas_status": "not_created",
        "latest_client_update_draft": "SECRET CLIENT DRAFT",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _document(**overrides):
    values = {
        "title": "Spec",
        "filename": "spec.pdf",
        "slack_file_id": "F1",
        "permalink": "https://slack.test/files/F1",
        "parse_status": "parsed",
        "analysis_status": "completed",
        "page_count": 3,
        "analysis_summary": "Short summary",
        "analysis_items": {},
        "extracted_text": "FULL_DOCUMENT_TEXT_SHOULD_NOT_APPEAR",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_canvas_omits_full_document_text_and_admin_draft():
    documents = [
        (
            _document(
                analysis_items={"requirements": [{"text": "Support refunds"}]},
            ),
            5,
        )
    ]

    markdown = canvas_service._render_dashboard(
        _project(),
        tasks=[],
        clarifications=[],
        task_suggestions=[],
        decisions=[],
        documents=documents,
    )

    assert "FULL_DOCUMENT_TEXT_SHOULD_NOT_APPEAR" not in markdown
    assert "SECRET CLIENT DRAFT" not in markdown
    assert "## Latest Client Update Draft" not in markdown
    assert "Short summary" in markdown
    assert "Support refunds" in markdown


def test_pending_client_section_excludes_developer_questions():
    clarifications = [
        SimpleNamespace(
            status="pending",
            question="DEV QUESTION about caching",
            final_answer=None,
            answered_by_slack_id=None,
            answered_at=None,
            payload={"submitter_slack_id": "U_DEV", "answer_owner": "client"},
            source_refs=[],
            created_at=NOW,
        ),
        SimpleNamespace(
            status="pending",
            question="CLIENT QUESTION about currency",
            final_answer=None,
            answered_by_slack_id=None,
            answered_at=None,
            payload={"answer_owner": "client"},
            source_refs=[],
            created_at=NOW,
        ),
    ]

    markdown = canvas_service._render_dashboard(
        _project(),
        tasks=[],
        clarifications=clarifications,
        task_suggestions=[],
        decisions=[],
        documents=[],
    )

    dev_section = markdown.split("## Developer Clarifications / Q&A")[1].split("## Pending Client Clarifications")[0]
    client_section = markdown.split("## Pending Client Clarifications")[1]

    # Dev-submitted question appears under the developer section, not the client one.
    assert "DEV QUESTION about caching" in dev_section
    assert "DEV QUESTION about caching" not in client_section
    assert "CLIENT QUESTION about currency" in client_section


def test_canvas_blocked_tasks_surface_blockers():
    blocked_suggestion = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"blocked_by": ["Waiting on payment gateway credentials"]},
    )
    tasks = [
        SimpleNamespace(
            title="Integrate gateway",
            status="blocked",
            priority="high",
            owner_slack_id=None,
            source_suggestion_id=blocked_suggestion.id,
            source_refs=[],
            created_at=NOW,
        )
    ]

    markdown = canvas_service._render_dashboard(
        _project(),
        tasks=tasks,
        clarifications=[],
        task_suggestions=[blocked_suggestion],
        decisions=[],
        documents=[],
    )

    blocked_section = markdown.split("## Blocked Tasks")[1].split("## Developer")[0]
    assert "Integrate gateway" in blocked_section
    assert "Waiting on payment gateway credentials" in blocked_section
