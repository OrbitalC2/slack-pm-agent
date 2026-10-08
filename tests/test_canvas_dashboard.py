import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from pm_agent.app.db.models import Project
from pm_agent.app.services import (
    canvas_service,
    gather_info_service,
    huddle_notes_service,
    project_service,
    slack_service,
    suggestion_approval_service,
)
from pm_agent.app.utils import permissions


NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _slack_error(error: str) -> SlackApiError:
    response = AsyncSlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.test/api",
        req_args={},
        data={"ok": False, "error": error},
        headers={},
        status_code=200,
    )
    return SlackApiError(error, response)


def _project(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Client Portal",
        "client_name": "Example Client",
        "client_channel_id": "C_CLIENT",
        "internal_channel_id": "C_INTERNAL",
        "github_repo_url": "https://github.com/example/portal",
        "project_summary": "Current project summary",
        "internal_canvas_id": None,
        "internal_canvas_status": "not_created",
        "latest_client_update_draft": "Client-safe update draft",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_canvas_migration_and_model_fields_are_complete():
    columns = Project.__table__.c
    assert {
        "internal_canvas_id",
        "internal_canvas_updated_at",
        "internal_canvas_status",
        "latest_client_update_draft",
        "latest_client_update_generated_at",
    }.issubset(columns.keys())
    assert columns.internal_canvas_status.server_default.arg.text == "'not_created'"

    migration = Path(
        "pm_agent/app/db/migrations/versions/008_project_canvas_dashboard.py"
    ).read_text()
    assert 'revision: str = "008_canvas_dashboard"' in migration
    assert 'down_revision: Union[str, None] = "007_documents"' in migration
    assert "ck_project_internal_canvas_status" in migration


def _document(**overrides):
    values = {
        "title": None,
        "filename": "requirements.pdf",
        "slack_file_id": "F1",
        "permalink": "https://slack.test/files/F1",
        "parse_status": "parsed",
        "analysis_status": "completed",
        "page_count": 4,
        "analysis_summary": "Doc summary",
        "analysis_items": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_dashboard_renders_dev_safe_sections_and_excludes_admin_content():
    project = _project(
        project_summary="S" * 1700,
        latest_client_update_draft="SECRET CLIENT DRAFT",
    )
    source_ref = [{"permalink": "https://slack.test/archives/C/p1"}]
    open_suggestion = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "assignee_text": "Ali | Backend",
            "acceptance_criteria": ["Returns 200", "Writes audit log"],
            "dependencies": ["Auth service"],
        },
    )
    blocked_suggestion = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"blocked_by": ["Need API key from client"]},
    )
    tasks = [
        SimpleNamespace(
            title="Build search endpoint",
            status="todo",
            priority="high",
            owner_slack_id=None,
            source_suggestion_id=open_suggestion.id,
            source_refs=source_ref,
            created_at=NOW,
        ),
        SimpleNamespace(
            title="Wire payment webhook",
            status="blocked",
            priority="urgent",
            owner_slack_id=None,
            source_suggestion_id=blocked_suggestion.id,
            source_refs=[],
            created_at=NOW + timedelta(minutes=1),
        ),
        SimpleNamespace(
            title="Completed task",
            status="done",
            priority="urgent",
            owner_slack_id="U_DONE",
            source_suggestion_id=None,
            source_refs=[],
            created_at=NOW,
        ),
    ]
    clarifications = [
        SimpleNamespace(
            status="pending",
            question="Should cancelled users keep invoice access?",
            final_answer=None,
            answered_by_slack_id=None,
            answered_at=None,
            payload={"submitter_slack_id": "U_DEV", "answer_owner": "admin"},
            source_refs=[],
            created_at=NOW,
        ),
        SimpleNamespace(
            status="pending",
            question="Which currencies must checkout support?",
            final_answer=None,
            answered_by_slack_id=None,
            answered_at=None,
            payload={
                "answer_owner": "client",
                "options": ["USD only", "USD + EUR"],
                "recommended_option": "USD + EUR for EU launch",
            },
            source_refs=[],
            created_at=NOW,
        ),
        SimpleNamespace(
            status="answered",
            question="What DB are we using?",
            final_answer="PostgreSQL 16",
            answered_by_slack_id="U_ADMIN",
            answered_at=NOW,
            payload={},
            source_refs=source_ref,
            created_at=NOW,
        ),
    ]
    decisions = [
        SimpleNamespace(
            title="Use a migration shim",
            payload={"channel_kind": "internal"},
            created_at=NOW,
        )
    ]
    documents = [
        (
            _document(
                analysis_summary="Spec overview",
                analysis_items={
                    "requirements": [{"text": "Must support SSO login"}],
                    "suggested_tasks": [{"title": "Implement SSO callback"}],
                    "clarifications": [{"question": "Which IdP?"}],
                },
            ),
            8,
        )
    ]

    markdown = canvas_service._render_dashboard(
        project,
        tasks,
        clarifications,
        [open_suggestion, blocked_suggestion],
        decisions,
        documents,
    )

    assert "## Project Overview" in markdown
    assert "## Project Summary" in markdown
    assert "## Confirmed Requirements" in markdown
    assert "## Open Tasks" in markdown
    assert "## Blocked Tasks" in markdown
    assert "## Developer Clarifications / Q&A" in markdown
    assert "## Pending Client Clarifications" in markdown
    assert "## Recent Implementation Decisions" in markdown
    assert "## Documents" in markdown

    # Admin-sensitive content is excluded.
    assert "## Latest Client Update Draft" not in markdown
    assert "SECRET CLIENT DRAFT" not in markdown
    assert "## Risks / Assumptions" not in markdown

    # Open tasks exclude done/blocked; include acceptance criteria + owner.
    assert "Build search endpoint" in markdown
    assert "Completed task" not in markdown
    assert "Returns 200" in markdown
    assert "Ali | Backend" in markdown

    # Blocked task surfaces its blocker.
    assert "Wire payment webhook" in markdown
    assert "Need API key from client" in markdown

    # Developer Q&A + answered questions.
    assert "Should cancelled users keep invoice access?" in markdown
    assert "PostgreSQL 16" in markdown
    assert "<@U_ADMIN>" in markdown

    # Pending client clarification shows options/recommended.
    assert "Which currencies must checkout support?" in markdown
    assert "USD + EUR for EU launch" in markdown

    assert "[Internal] Use a migration shim" in markdown

    # Documents section: requirement (sourced), summary, implementation points.
    assert "Must support SSO login" in markdown
    assert "[source](https://slack.test/files/F1)" in markdown
    assert "Spec overview" in markdown
    assert "Implement SSO callback" in markdown

    assert "S" * 1501 not in markdown


def test_dashboard_limits_open_items_and_adds_notice():
    tasks = [
        SimpleNamespace(
            title=f"Task {index}",
            status="todo",
            priority="medium",
            owner_slack_id=None,
            source_suggestion_id=None,
            source_refs=[],
            created_at=NOW + timedelta(minutes=index),
        )
        for index in range(21)
    ]
    markdown = canvas_service._render_dashboard(
        _project(), tasks, [], [], [], []
    )

    assert "Task 19" in markdown
    assert "Task 20" not in markdown
    assert "_Showing top 20 items. Use /agent status for more._" in markdown


def test_slack_canvas_wrappers_use_markdown_and_whole_document_replace():
    calls = {}

    class Client:
        async def conversations_canvases_create(self, **kwargs):
            calls["create"] = kwargs
            return {"canvas_id": "F_CANVAS"}

        async def canvases_edit(self, **kwargs):
            calls["edit"] = kwargs
            return {"ok": True}

    canvas_id, adopted = asyncio.run(
        slack_service.create_channel_canvas(
            Client(), "C_INTERNAL", "Project Dashboard", "# Dashboard"
        )
    )
    asyncio.run(slack_service.edit_canvas(Client(), canvas_id, "# Updated"))

    assert (canvas_id, adopted) == ("F_CANVAS", False)
    assert calls["create"]["channel_id"] == "C_INTERNAL"
    assert calls["create"]["document_content"] == {
        "type": "markdown",
        "markdown": "# Dashboard",
    }
    assert calls["edit"]["changes"] == [
        {
            "operation": "replace",
            "document_content": {"type": "markdown", "markdown": "# Updated"},
        }
    ]


def test_existing_channel_canvas_is_discovered_and_adopted():
    class Client:
        async def conversations_canvases_create(self, **kwargs):
            raise _slack_error("channel_canvas_already_exists")

        async def conversations_info(self, **kwargs):
            return {
                "channel": {
                    "properties": {"canvas": {"canvas_id": "F_EXISTING"}}
                }
            }

    result = asyncio.run(
        slack_service.create_channel_canvas(
            Client(), "C_INTERNAL", "Dashboard", "content"
        )
    )

    assert result == ("F_EXISTING", True)


def test_adopted_canvas_is_replaced(monkeypatch):
    project = _project()
    edits = []

    async def get_project(db, project_id):
        return project

    async def create(*args, **kwargs):
        return "F_EXISTING", True

    async def edit(*args, **kwargs):
        edits.append(args)

    monkeypatch.setattr(canvas_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(canvas_service, "get_project_by_id", get_project)
    monkeypatch.setattr(canvas_service, "create_channel_canvas", create)
    monkeypatch.setattr(canvas_service, "edit_canvas", edit)

    canvas_id = asyncio.run(
        canvas_service._create_internal_channel_canvas(
            project.id, object(), "# Dashboard"
        )
    )

    assert canvas_id == "F_EXISTING"
    assert edits[0][1:] == ("F_EXISTING", "# Dashboard")


def test_canvas_sync_never_targets_client_channel(monkeypatch):
    project = _project(internal_channel_id="C_CLIENT")
    create_calls = []

    async def get_project(db, project_id):
        return project

    async def render(project_id):
        return "dashboard"

    async def create(*args, **kwargs):
        create_calls.append(args)
        return "F1", False

    states = []

    async def persist(*args, **kwargs):
        states.append(kwargs["status"])

    monkeypatch.setattr(canvas_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(canvas_service, "get_project_by_id", get_project)
    monkeypatch.setattr(canvas_service, "render_project_canvas", render)
    monkeypatch.setattr(canvas_service, "create_channel_canvas", create)
    monkeypatch.setattr(canvas_service, "_persist_canvas_state", persist)
    canvas_service._project_locks.clear()

    result = asyncio.run(
        canvas_service.sync_project_canvas(project.id, "test", client=object())
    )

    assert result is False
    assert create_calls == []
    assert states == ["failed"]


def test_canvas_failure_is_recorded_without_escaping(monkeypatch):
    project_id = uuid.uuid4()
    states = []

    async def fail(*args, **kwargs):
        raise RuntimeError("Canvas unavailable")

    async def persist(*args, **kwargs):
        states.append(kwargs["status"])

    monkeypatch.setattr(canvas_service, "_sync_project_canvas", fail)
    monkeypatch.setattr(canvas_service, "_persist_canvas_state", persist)
    canvas_service._project_locks.clear()

    result = asyncio.run(
        canvas_service.sync_project_canvas(project_id, "approval", client=object())
    )

    assert result is False
    assert states == ["failed"]


def test_stale_canvas_id_is_recreated_once(monkeypatch):
    project = _project(internal_canvas_id="F_STALE")
    states = []
    edits = []

    async def get_project(db, project_id):
        return project

    async def render(project_id):
        return "# Dashboard"

    async def edit(client, canvas_id, markdown):
        edits.append(canvas_id)
        raise _slack_error("canvas_not_found")

    async def create(project_id, client, markdown):
        return "F_NEW"

    async def persist(*args, **kwargs):
        states.append(kwargs)

    monkeypatch.setattr(canvas_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(canvas_service, "get_project_by_id", get_project)
    monkeypatch.setattr(canvas_service, "render_project_canvas", render)
    monkeypatch.setattr(canvas_service, "edit_canvas", edit)
    monkeypatch.setattr(canvas_service, "_create_internal_channel_canvas", create)
    monkeypatch.setattr(canvas_service, "_persist_canvas_state", persist)

    result = asyncio.run(
        canvas_service._sync_project_canvas(project.id, "recovery", object())
    )

    assert result is True
    assert edits == ["F_STALE"]
    assert states[0]["status"] == "not_created"
    assert states[0]["canvas_id"] is None
    assert states[1]["status"] == "created"
    assert states[1]["canvas_id"] == "F_NEW"


def test_same_project_canvas_syncs_are_serialized(monkeypatch):
    project_id = uuid.uuid4()
    active = 0
    peak = 0

    async def sync(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return True

    monkeypatch.setattr(canvas_service, "_sync_project_canvas", sync)
    canvas_service._project_locks.clear()

    async def run():
        return await asyncio.gather(
            canvas_service.sync_project_canvas(project_id, "one", client=object()),
            canvas_service.sync_project_canvas(project_id, "two", client=object()),
        )

    assert asyncio.run(run()) == [True, True]
    assert peak == 1


def test_internal_channel_remap_clears_canvas_metadata(monkeypatch):
    existing = _project(
        internal_channel_id="C_OLD",
        internal_canvas_id="F_OLD",
        internal_canvas_status="created",
    )
    captured = {}

    async def find_existing(*args, **kwargs):
        return existing

    async def update(db, project, **kwargs):
        captured.update(kwargs)
        for key, value in kwargs.items():
            setattr(project, key, value)
        return project

    monkeypatch.setattr(project_service, "_find_existing_project_for_setup", find_existing)
    monkeypatch.setattr(project_service, "is_global_admin", lambda user_id: True)
    monkeypatch.setattr(project_service, "update_project", update)

    project, updated, error = asyncio.run(
        project_service._save_project(
            db=object(),
            user_id="U_ADMIN",
            current_channel_id="C_OLD",
            name="Client Portal",
            client_channel_id="C_CLIENT",
            client_name=None,
            internal_channel_id="C_NEW",
            internal_channel_was_provided=True,
            github_url=None,
        )
    )

    assert error is None and updated is True
    assert project.internal_channel_id == "C_NEW"
    assert captured["internal_canvas_id"] is None
    assert captured["internal_canvas_updated_at"] is None
    assert captured["internal_canvas_status"] == "not_created"


def test_setup_success_is_preserved_when_canvas_sync_fails(monkeypatch):
    project = _project()
    syncs = []

    async def no_existing(*args, **kwargs):
        return None

    async def create(*args, **kwargs):
        return project

    async def sync(project_id, reason, client=None):
        syncs.append((project_id, reason))
        return False

    monkeypatch.setattr(project_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(project_service, "_find_existing_project_for_setup", no_existing)
    monkeypatch.setattr(project_service, "is_global_admin", lambda user_id: True)
    monkeypatch.setattr(project_service, "create_project", create)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    result = asyncio.run(
        project_service.setup_project(
            "U_ADMIN",
            "C_INTERNAL",
            'name="Client Portal" client_channel=C_CLIENT',
            object(),
        )
    )

    assert "project saved" in result.lower()
    assert syncs == [(project.id, "project_setup")]


def test_task_and_clarification_approval_trigger_canvas_sync(monkeypatch):
    project = _project()
    syncs = []
    sent = []

    async def claim(db, suggestion_id, user_id, client, body=None):
        suggestion_type = "task" if not sent else "clarification"
        return (
            SimpleNamespace(
                id=suggestion_id,
                project_id=project.id,
                title=f"{suggestion_type} title",
                body="details",
                payload={"priority": "high"},
                source_message_ids=[],
                source_refs=[],
            ),
            project,
        )

    async def insert_task(db, **kwargs):
        return SimpleNamespace(
            title=kwargs["title"],
            description=kwargs["description"],
            owner_slack_id=None,
            priority=kwargs["priority"],
        )

    async def insert_clarification(db, **kwargs):
        return SimpleNamespace(**kwargs)

    async def send_dm(*args, **kwargs):
        sent.append(kwargs.get("text"))

    async def sync(project_id, reason, client=None):
        syncs.append((project_id, reason))
        return True

    monkeypatch.setattr(
        suggestion_approval_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(suggestion_approval_service, "_claim_suggestion", claim)
    monkeypatch.setattr(suggestion_approval_service, "insert_task", insert_task)
    monkeypatch.setattr(
        suggestion_approval_service, "insert_clarification", insert_clarification
    )
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    task_id = uuid.uuid4()
    clarification_id = uuid.uuid4()
    body = {"user": {"id": "U_ADMIN"}}
    asyncio.run(
        suggestion_approval_service.approve_task_suggestion(
            {"value": f'{{"suggestion_id":"{task_id}"}}'}, body, object()
        )
    )
    asyncio.run(
        suggestion_approval_service.approve_clarification_suggestion(
            {"value": f'{{"suggestion_id":"{clarification_id}"}}'}, body, object()
        )
    )

    assert syncs == [
        (project.id, "task_approved"),
        (project.id, "clarification_approved"),
    ]


def test_bulk_decision_risk_and_assumption_actions_trigger_canvas_sync(monkeypatch):
    project = _project()
    gather_run_id = uuid.uuid4()
    syncs = []
    suggestion = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project.id,
        title="Approved item",
        body="details",
        payload={"priority": "medium"},
        source_message_ids=[],
        source_refs=[],
    )

    async def pending(db, run_id):
        return [suggestion]

    async def get_project(db, project_id):
        return project

    async def claim_pending(db, suggestion_id, user_id):
        return suggestion

    async def claim(db, suggestion_id, user_id, client, body=None):
        return suggestion, project

    async def insert_task(db, **kwargs):
        return SimpleNamespace(
            title=kwargs["title"],
            description=kwargs["description"],
            owner_slack_id=None,
            priority=kwargs["priority"],
        )

    async def noop(*args, **kwargs):
        return None

    async def sync(project_id, reason, client=None):
        syncs.append((project_id, reason))
        return True

    monkeypatch.setattr(
        suggestion_approval_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(
        suggestion_approval_service, "get_pending_tasks_for_gather_run", pending
    )
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(
        suggestion_approval_service, "can_approve_suggestion", lambda user_id: True
    )
    monkeypatch.setattr(
        suggestion_approval_service, "claim_pending_suggestion", claim_pending
    )
    monkeypatch.setattr(suggestion_approval_service, "_claim_suggestion", claim)
    monkeypatch.setattr(suggestion_approval_service, "insert_task", insert_task)
    monkeypatch.setattr(suggestion_approval_service, "embed_and_store", noop)
    monkeypatch.setattr(suggestion_approval_service, "send_dm", noop)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    body = {"user": {"id": "U_ADMIN"}}
    asyncio.run(
        suggestion_approval_service.approve_all_tasks(
            {"value": f'{{"gather_run_id":"{gather_run_id}"}}'}, body, object()
        )
    )
    action = {"value": f'{{"suggestion_id":"{suggestion.id}"}}'}
    asyncio.run(
        suggestion_approval_service.approve_decision_suggestion(action, body, object())
    )
    asyncio.run(suggestion_approval_service.acknowledge_scope_risk(action, body, object()))
    asyncio.run(suggestion_approval_service.acknowledge_assumption(action, body, object()))

    assert syncs == [
        (project.id, "tasks_approved"),
        (project.id, "decision_approved"),
        (project.id, "scope_risk_acknowledged"),
        (project.id, "assumption_acknowledged"),
    ]


def test_no_message_gather_completion_triggers_canvas_sync(monkeypatch):
    project = _project(project_manager_slack_id="U_ADMIN")
    syncs = []

    class GatherSession(FakeSession):
        def add(self, item):
            self.item = item

        async def flush(self):
            if getattr(self.item, "id", None) is None:
                self.item.id = uuid.uuid4()

    async def find_project(db, channel_id):
        return project

    async def latest(*args):
        return None

    async def messages(*args):
        return []

    async def history(*args, **kwargs):
        return []

    async def no_carryover(*args, **kwargs):
        return []

    async def complete(*args, **kwargs):
        return None

    async def dm(*args, **kwargs):
        return None

    async def sync(project_id, reason, client=None):
        syncs.append((project_id, reason))
        return True

    monkeypatch.setattr(
        gather_info_service, "get_session_factory", lambda: lambda: GatherSession()
    )
    monkeypatch.setattr(gather_info_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(
        gather_info_service, "can_run_command", lambda user_id, command_name: True
    )
    monkeypatch.setattr(
        gather_info_service, "get_project_channel_kind", lambda target, channel: "internal"
    )
    monkeypatch.setattr(
        gather_info_service, "get_latest_completed_gather_for_channel", latest
    )
    monkeypatch.setattr(gather_info_service, "get_messages_since", messages)
    monkeypatch.setattr(gather_info_service, "fetch_channel_history", history)
    monkeypatch.setattr(
        gather_info_service, "get_unconsumed_completed_documents", no_carryover
    )
    monkeypatch.setattr(gather_info_service, "mark_gather_completed", complete)
    monkeypatch.setattr(gather_info_service, "_safe_send_dm", dm)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    asyncio.run(
        gather_info_service.run_gather_info("U_ADMIN", "C_INTERNAL", object())
    )

    assert syncs == [(project.id, "gather_completed")]


def test_huddle_processing_triggers_canvas_sync(monkeypatch):
    project = _project(project_manager_slack_id="U_ADMIN")
    syncs = []

    async def find_project(db, channel_id):
        return project

    async def chain(*args, **kwargs):
        return SimpleNamespace(
            summary="Huddle summary", decisions=[], tasks=[], clarifications=[]
        )

    async def insert_run(*args, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def memory(*args, **kwargs):
        return None

    async def dm(*args, **kwargs):
        return None

    async def sync(project_id, reason, client=None):
        syncs.append((project_id, reason))
        return True

    monkeypatch.setattr(
        huddle_notes_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(huddle_notes_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(
        huddle_notes_service, "can_approve_suggestion", lambda user_id: True
    )
    monkeypatch.setattr(huddle_notes_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(huddle_notes_service, "huddle_notes_chain", chain)
    monkeypatch.setattr(huddle_notes_service, "insert_huddle_run", insert_run)
    monkeypatch.setattr(huddle_notes_service, "embed_and_store", memory)
    monkeypatch.setattr(
        huddle_notes_service, "huddle_summary_block_batches", lambda *args: [[]]
    )
    monkeypatch.setattr(huddle_notes_service, "send_dm", dm)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    asyncio.run(
        huddle_notes_service._process_huddle_notes(
            "U_ADMIN", "C_INTERNAL", "notes", None, object()
        )
    )

    assert syncs == [(project.id, "huddle_processed")]


def test_dev_can_open_huddle_modal_for_configured_project(monkeypatch):
    project = _project(project_manager_slack_id="U_ADMIN")
    opened = []
    dms = []

    async def find_project(db, channel_id):
        return project

    async def open_modal(client, trigger_id, view):
        opened.append((trigger_id, view))

    async def send_dm(client, user_id, text="", blocks=None):
        dms.append((user_id, text, blocks))

    monkeypatch.setattr(
        huddle_notes_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(huddle_notes_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(huddle_notes_service, "open_modal", open_modal)
    monkeypatch.setattr(huddle_notes_service, "send_dm", send_dm)
    monkeypatch.setattr(
        huddle_notes_service,
        "is_project_approver",
        lambda *args: False,
        raising=False,
    )

    asyncio.run(
        huddle_notes_service.open_huddle_modal(
            object(), "TRIGGER", "C_INTERNAL", user_id="U_DEV"
        )
    )

    assert opened
    assert dms == []


def test_dev_huddle_processing_routes_approval_blocks_to_admins(monkeypatch):
    project = _project(project_manager_slack_id="U_ADMIN")
    sent_dms = []
    inserted_payloads = []

    async def find_project(db, channel_id):
        return project

    async def chain(*args, **kwargs):
        return SimpleNamespace(
            summary="Huddle summary",
            decisions=[],
            tasks=[
                SimpleNamespace(
                    title="Build invoice download",
                    description="Add PDF export",
                    assigned_to="U_DEV",
                    priority="urgent",
                )
            ],
            clarifications=[],
        )

    async def insert_run(*args, **kwargs):
        return SimpleNamespace(id=uuid.uuid4())

    async def insert_suggestion(*args, **kwargs):
        inserted_payloads.append(kwargs["payload"])
        return SimpleNamespace(
            id=uuid.uuid4(),
            suggestion_type=kwargs["suggestion_type"],
            title=kwargs["title"],
            body=kwargs["body"],
            source_message_ids=[],
            source_refs=kwargs["source_refs"],
            payload=kwargs["payload"],
        )

    async def memory(*args, **kwargs):
        return None

    async def send_dm(client, user_id, text="", blocks=None):
        sent_dms.append((user_id, text, blocks))

    async def sync(project_id, reason, client=None):
        return True

    monkeypatch.setattr(
        huddle_notes_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(huddle_notes_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(huddle_notes_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(huddle_notes_service, "huddle_notes_chain", chain)
    monkeypatch.setattr(huddle_notes_service, "insert_huddle_run", insert_run)
    monkeypatch.setattr(huddle_notes_service, "insert_suggestion", insert_suggestion)
    monkeypatch.setattr(huddle_notes_service, "embed_and_store", memory)
    monkeypatch.setattr(huddle_notes_service, "send_dm", send_dm)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))
    monkeypatch.setattr(
        huddle_notes_service,
        "is_project_approver",
        lambda *args: False,
        raising=False,
    )

    asyncio.run(
        huddle_notes_service._process_huddle_notes(
            "U_DEV", "C_INTERNAL", "notes", None, object()
        )
    )

    assert inserted_payloads[0]["submitted_by_slack_id"] == "U_DEV"
    submitter_messages = [dm for dm in sent_dms if dm[0] == "U_DEV"]
    admin_messages = [dm for dm in sent_dms if dm[0] == "U_ADMIN"]
    assert submitter_messages
    assert all(blocks is None for _, _, blocks in submitter_messages)
    assert any(blocks for _, _, blocks in admin_messages)


def test_project_update_is_persisted_before_canvas_sync_and_dm(monkeypatch):
    from pm_agent.app.agents import chains, llm_client

    project = _project(project_manager_slack_id="U_ADMIN")
    events = []

    async def find_project(db, channel_id):
        return project

    async def get_project(db, project_id):
        return project

    async def summary(*args):
        return {"done": 0, "doing": 0, "blocked": 0, "review": 0, "todo": 0, "tasks": []}

    async def empty(*args):
        return []

    async def update(db, target, **kwargs):
        events.append(("persist", kwargs))
        for key, value in kwargs.items():
            setattr(target, key, value)
        return target

    async def update_chain(*args):
        return SimpleNamespace(
            summary="Ready", completed_items=[], in_progress=[], blockers=[], next_steps=[]
        )

    async def sync(project_id, reason, client=None):
        events.append(("sync", reason))
        return True

    async def dm(*args, **kwargs):
        events.append(("dm", kwargs["text"]))

    monkeypatch.setattr(project_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(project_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(project_service, "get_project_by_id", get_project)
    monkeypatch.setattr(project_service, "get_task_summary", summary)
    monkeypatch.setattr(project_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(project_service, "get_approved_suggestions", empty)
    monkeypatch.setattr(project_service, "update_project", update)
    monkeypatch.setattr(project_service, "is_global_admin", lambda user_id: True)
    monkeypatch.setattr(slack_service, "get_slack_client", lambda: object())
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: object())
    monkeypatch.setattr(chains, "project_update_chain", update_chain)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)
    monkeypatch.setattr(project_service, "send_dm", dm)

    asyncio.run(project_service.generate_project_update("U_ADMIN", "C_INTERNAL"))

    assert [event[0] for event in events] == ["persist", "sync", "dm"]
    assert project.latest_client_update_draft
    assert project.latest_client_update_generated_at is not None
