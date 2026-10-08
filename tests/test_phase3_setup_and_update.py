import asyncio
from types import SimpleNamespace

from pm_agent.app.services import project_service
from pm_agent.app.utils.channel_refs import extract_channel_id, extract_channel_name
from pm_agent.app.utils.project_update_draft import (
    build_project_update_data,
    client_safe_decisions,
    format_fallback_project_update,
    format_project_update_draft,
)
from pm_agent.app.utils.slack_formatting import setup_project_modal


def test_channel_ref_extracts_ids_and_names():
    assert extract_channel_id("C123") == "C123"
    assert extract_channel_id("G123") == "G123"
    assert extract_channel_id("<#C123|client-project>") == "C123"
    assert extract_channel_id("#client-project") is None

    assert extract_channel_name("#client-project") == "client-project"
    assert extract_channel_name("C123") is None


def test_setup_modal_requires_explicit_client_channel_without_silent_default():
    modal = setup_project_modal("C_CURRENT")

    client_block = next(block for block in modal["blocks"] if block["block_id"] == "client_channel")

    assert modal["submit"]["text"] == "Save Project"
    assert client_block["element"]["type"] == "conversations_select"
    assert "initial_conversation" not in client_block["element"]


def test_project_update_data_includes_tasks_github_and_approved_scope_items():
    task_summary = {
        "done": 1,
        "doing": 1,
        "blocked": 1,
        "review": 0,
        "todo": 0,
        "tasks": [
            SimpleNamespace(title="Finish exports", status="done"),
            SimpleNamespace(title="Build import preview", status="doing"),
            SimpleNamespace(title="Waiting on CSV sample", status="blocked"),
        ],
    }

    project_data = build_project_update_data(
        project_name="Client Portal",
        task_summary=task_summary,
        pending_clarifications=[SimpleNamespace(question="Which CSV columns are required?")],
        approved_decisions=[SimpleNamespace(title="Use Stripe", body="")],
        approved_risks=[SimpleNamespace(title="CSV sample missing", body="May block import QA")],
        approved_assumptions=[SimpleNamespace(title="Admins export reports", body="Needs client confirmation")],
        github_summary="*Recent Commits:*\n- abc123 Finish export endpoint",
    )

    assert "Project: Client Portal" in project_data
    assert "Completed tasks:\n- Finish exports" in project_data
    assert "Pending client clarifications:\n- Which CSV columns are required?" in project_data
    assert "Approved decisions:\n- Use Stripe" in project_data
    assert "Acknowledged scope risks:\n- CSV sample missing: May block import QA" in project_data
    assert "GitHub activity:" in project_data
    assert "abc123" in project_data


def test_internal_decisions_are_excluded_from_client_update_context():
    client_decision = SimpleNamespace(title="Client chose Stripe", payload={"channel_kind": "client"})
    internal_decision = SimpleNamespace(
        title="Use temporary migration shim",
        payload={"channel_kind": "internal"},
    )
    decisions = client_safe_decisions([client_decision, internal_decision])
    project_data = build_project_update_data(
        project_name="Client Portal",
        task_summary={"done": 0, "doing": 0, "blocked": 0, "review": 0, "todo": 0, "tasks": []},
        pending_clarifications=[],
        approved_decisions=decisions,
        approved_risks=[],
        approved_assumptions=[],
        github_summary="",
    )

    assert "Client chose Stripe" in project_data
    assert "Use temporary migration shim" not in project_data


def test_project_update_draft_is_private_review_copy():
    output = SimpleNamespace(
        summary="We completed exports and are working on import preview.",
        completed_items=["Exports are ready"],
        in_progress=["Import preview"],
        blockers=["Need CSV sample"],
        next_steps=["Validate sample file"],
    )

    draft = format_project_update_draft("Client Portal", output)

    assert "Draft Update: Client Portal" in draft
    assert "This was not sent automatically" in draft
    assert "Exports are ready" in draft
    assert "Need CSV sample" in draft


def test_fallback_project_update_includes_github_activity():
    task_summary = {
        "done": 1,
        "doing": 0,
        "blocked": 0,
        "tasks": [SimpleNamespace(title="Finish exports", status="done")],
    }

    draft = format_fallback_project_update(
        "Client Portal",
        task_summary,
        pending_clarifications=[],
        github_summary="No recent GitHub activity found.",
    )

    assert "Finish exports" in draft
    assert "GitHub Activity" in draft
    assert "No recent GitHub activity found." in draft


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _patch_inline_setup_persistence(monkeypatch):
    created = {}

    async def no_existing(*args, **kwargs):
        return None

    async def create_project(*args, **kwargs):
        created.update(kwargs)
        return SimpleNamespace(
            name=kwargs["name"],
            client_name=kwargs["client_name"],
            client_channel_id=kwargs["client_channel_id"],
            internal_channel_id=kwargs["internal_channel_id"],
            github_repo_url=kwargs["github_repo_url"],
        )

    monkeypatch.setattr(project_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(project_service, "_find_existing_project_for_setup", no_existing)
    monkeypatch.setattr(project_service, "is_global_admin", lambda user_id: True)
    monkeypatch.setattr(project_service, "create_project", create_project)
    return created


def test_inline_setup_requires_client_channel(monkeypatch):
    created = _patch_inline_setup_persistence(monkeypatch)

    result = asyncio.run(
        project_service.setup_project(
            user_id="U_ADMIN",
            channel_id="C_CLIENT",
            args_text='name="Project"',
            client=object(),
        )
    )

    assert "`client_channel` is required" in result
    assert created == {}


def test_inline_setup_from_internal_channel_infers_internal_channel(monkeypatch):
    created = _patch_inline_setup_persistence(monkeypatch)

    asyncio.run(
        project_service.setup_project(
            user_id="U_ADMIN",
            channel_id="C_INTERNAL",
            args_text='name="Project" client_channel=C_CLIENT',
            client=object(),
        )
    )

    assert created["client_channel_id"] == "C_CLIENT"
    assert created["internal_channel_id"] == "C_INTERNAL"


def test_inline_setup_from_client_channel_does_not_set_internal_channel(monkeypatch):
    created = _patch_inline_setup_persistence(monkeypatch)

    asyncio.run(
        project_service.setup_project(
            user_id="U_ADMIN",
            channel_id="C_CLIENT",
            args_text='name="Project" client_channel=C_CLIENT',
            client=object(),
        )
    )

    assert created["client_channel_id"] == "C_CLIENT"
    assert created["internal_channel_id"] is None


def test_inline_setup_explicit_internal_channel_wins(monkeypatch):
    created = _patch_inline_setup_persistence(monkeypatch)

    asyncio.run(
        project_service.setup_project(
            user_id="U_ADMIN",
            channel_id="C_RANDOM",
            args_text='name="Project" client_channel=C_CLIENT internal_channel=C_INTERNAL',
            client=object(),
        )
    )

    assert created["client_channel_id"] == "C_CLIENT"
    assert created["internal_channel_id"] == "C_INTERNAL"


def test_inline_setup_rejects_same_client_and_internal_channel(monkeypatch):
    created = _patch_inline_setup_persistence(monkeypatch)

    result = asyncio.run(
        project_service.setup_project(
            user_id="U_ADMIN",
            channel_id="C_CLIENT",
            args_text='name="Project" client_channel=C_CLIENT internal_channel=C_CLIENT',
            client=object(),
        )
    )

    assert "must be different" in result
    assert created == {}
