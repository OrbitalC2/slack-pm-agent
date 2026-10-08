"""Tests for the `/agent clarify` developer clarification command."""

import asyncio
import uuid
from types import SimpleNamespace

from pm_agent.app.services import clarification_service
from pm_agent.app.utils.permissions import DEV_ASK_RESTRICTED_MESSAGE


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _project():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Client Portal",
        internal_channel_id="C_INTERNAL",
        client_channel_id="C_CLIENT",
        project_summary="A billing portal",
    )


def _decision():
    return SimpleNamespace(
        unclear_point="Should cancelled users keep invoice access?",
        why_it_matters="Affects retention logic",
        known_context="Billing is new",
        options=["Yes, read-only", "No access"],
        recommended_option="Yes, read-only for 30 days",
        answer_owner="client",
        confidence="medium",
    )


def _base_monkeypatch(monkeypatch, project, *, dev_allowed=True):
    monkeypatch.setattr(
        clarification_service, "get_session_factory", lambda: lambda: FakeSession()
    )

    async def find_project(db, channel_id):
        return project

    async def is_allowed(user_id, question, project_context=None):
        return dev_allowed

    async def get_tasks(db, project_id):
        return []

    monkeypatch.setattr(clarification_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(clarification_service, "is_dev_ask_allowed", is_allowed)
    monkeypatch.setattr(clarification_service, "get_tasks", get_tasks)
    monkeypatch.setattr(
        clarification_service, "get_global_admin_slack_ids", lambda: ["U_ADMIN"]
    )


def test_clarify_requires_internal_channel(monkeypatch):
    project = _project()
    _base_monkeypatch(monkeypatch, project)

    async def get_clarifications(db, project_id, status=None):
        return []

    async def get_pending(db, project_id, stype=None):
        return []

    monkeypatch.setattr(clarification_service, "get_clarifications", get_clarifications)
    monkeypatch.setattr(clarification_service, "get_pending_suggestions", get_pending)

    result = asyncio.run(
        clarification_service.handle_clarify_command(
            "U_DEV", "C_CLIENT", "Some question?", object()
        )
    )
    assert result == clarification_service.CLARIFY_INTERNAL_ONLY


def test_clarify_blocks_restricted_dev_question(monkeypatch):
    project = _project()
    _base_monkeypatch(monkeypatch, project, dev_allowed=False)

    result = asyncio.run(
        clarification_service.handle_clarify_command(
            "U_DEV", "C_INTERNAL", "What should we tell the client?", object()
        )
    )
    assert result == DEV_ASK_RESTRICTED_MESSAGE


def test_clarify_returns_existing_answer_without_creating(monkeypatch):
    project = _project()
    _base_monkeypatch(monkeypatch, project)

    answered = [
        SimpleNamespace(
            question="What database should we use?",
            final_answer="PostgreSQL 16",
            answered_by_slack_id="U_ADMIN",
            status="answered",
        )
    ]

    async def get_clarifications(db, project_id, status=None):
        if status == "answered":
            return answered
        return []

    async def get_pending(db, project_id, stype=None):
        return []

    def fail_insert(*args, **kwargs):
        raise AssertionError("should not create a clarification")

    dms = []

    async def send_dm(client, user_id, text="", blocks=None):
        dms.append(user_id)

    monkeypatch.setattr(clarification_service, "get_clarifications", get_clarifications)
    monkeypatch.setattr(clarification_service, "get_pending_suggestions", get_pending)
    monkeypatch.setattr(clarification_service, "insert_suggestion", fail_insert)
    monkeypatch.setattr(clarification_service, "send_dm", send_dm)

    result = asyncio.run(
        clarification_service.handle_clarify_command(
            "U_DEV", "C_INTERNAL", "What database should we use?", object()
        )
    )

    assert "PostgreSQL 16" in result
    assert dms == []  # no admin DM when an answer already exists


def test_clarify_skips_duplicate_pending(monkeypatch):
    project = _project()
    _base_monkeypatch(monkeypatch, project)

    pending_clar = [
        SimpleNamespace(question="Which currencies must checkout support?", status="pending")
    ]

    async def get_clarifications(db, project_id, status=None):
        if status == "pending":
            return pending_clar
        return []

    async def get_pending(db, project_id, stype=None):
        return []

    async def no_confident(project_id, question, context_text):
        return None

    def fail_insert(*args, **kwargs):
        raise AssertionError("should not create a duplicate clarification")

    monkeypatch.setattr(clarification_service, "get_clarifications", get_clarifications)
    monkeypatch.setattr(clarification_service, "get_pending_suggestions", get_pending)
    monkeypatch.setattr(clarification_service, "_confident_answer", no_confident)
    monkeypatch.setattr(clarification_service, "insert_suggestion", fail_insert)

    result = asyncio.run(
        clarification_service.handle_clarify_command(
            "U_DEV", "C_INTERNAL", "Which currencies must checkout support?", object()
        )
    )

    assert "already pending" in result.lower()


def test_clarify_with_no_admins_does_not_create_request(monkeypatch):
    project = _project()
    _base_monkeypatch(monkeypatch, project)

    async def get_clarifications(db, project_id, status=None):
        return []

    async def get_pending(db, project_id, stype=None):
        return []

    async def no_confident(project_id, question, context_text):
        return None

    async def build_should_not_run(*args, **kwargs):
        raise AssertionError("should not build request when no admins can review it")

    def insert_should_not_run(*args, **kwargs):
        raise AssertionError("should not create an unreviewable clarification")

    monkeypatch.setattr(clarification_service, "get_clarifications", get_clarifications)
    monkeypatch.setattr(clarification_service, "get_pending_suggestions", get_pending)
    monkeypatch.setattr(clarification_service, "_confident_answer", no_confident)
    monkeypatch.setattr(clarification_service, "get_global_admin_slack_ids", lambda: [])
    monkeypatch.setattr(
        clarification_service, "build_decision_request_chain", build_should_not_run
    )
    monkeypatch.setattr(clarification_service, "insert_suggestion", insert_should_not_run)

    result = asyncio.run(
        clarification_service.handle_clarify_command(
            "U_DEV", "C_INTERNAL", "Should cancelled users keep invoice access?", object()
        )
    )

    assert result == clarification_service.NO_ADMINS_CONFIGURED


def test_clarify_creates_decision_request_and_dms_admin(monkeypatch):
    project = _project()
    _base_monkeypatch(monkeypatch, project)

    inserted = {}
    dms = []

    async def get_clarifications(db, project_id, status=None):
        return []

    async def get_pending(db, project_id, stype=None):
        return []

    async def no_confident(project_id, question, context_text):
        return None

    async def build_decision_request(question, context, llm_client):
        return _decision()

    async def insert_suggestion(db, **kwargs):
        inserted.update(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=kwargs["title"],
            body=kwargs["body"],
            payload=kwargs["payload"],
        )

    async def send_dm(client, user_id, text="", blocks=None):
        dms.append((user_id, blocks))

    monkeypatch.setattr(clarification_service, "get_clarifications", get_clarifications)
    monkeypatch.setattr(clarification_service, "get_pending_suggestions", get_pending)
    monkeypatch.setattr(clarification_service, "_confident_answer", no_confident)
    monkeypatch.setattr(
        clarification_service, "build_decision_request_chain", build_decision_request
    )
    monkeypatch.setattr(clarification_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(clarification_service, "insert_suggestion", insert_suggestion)
    monkeypatch.setattr(clarification_service, "send_dm", send_dm)

    result = asyncio.run(
        clarification_service.handle_clarify_command(
            "U_DEV", "C_INTERNAL", "Should cancelled users keep invoice access?", object()
        )
    )

    assert inserted["suggestion_type"] == "clarification"
    assert inserted["payload"]["submitter_slack_id"] == "U_DEV"
    assert inserted["payload"]["recommended_option"] == "Yes, read-only for 30 days"
    assert dms and dms[0][0] == "U_ADMIN"
    assert dms[0][1]  # admin DM includes decision-request blocks
    assert "created a clarification request" in result.lower()
