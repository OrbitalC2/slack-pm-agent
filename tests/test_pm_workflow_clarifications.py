"""Tests for the decision-request clarification flow and answer-internally."""

import asyncio
import json
import uuid
from types import SimpleNamespace

from pm_agent.app.services import canvas_service, suggestion_approval_service
from pm_agent.app.utils.slack_formatting import (
    clarification_answer_modal,
    suggestion_blocks,
)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


def _section_texts(blocks):
    for block in blocks:
        if block.get("type") == "section":
            yield block["text"]["text"]


def _clarification_dict():
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "suggestion_type": "clarification",
        "title": "Should cancelled users keep invoice access?",
        "body": "",
        "source_message_ids": [],
        "source_refs": [],
        "payload": {
            "why_it_matters": "Determines retention logic",
            "known_context": "Billing module is new",
            "options": ["Yes, read-only", "No access"],
            "recommended_option": "Yes, read-only for 30 days",
            "answer_owner": "client",
            "confidence": "medium",
        },
        "gather_run_id": "00000000-0000-0000-0000-000000000002",
    }


def test_clarification_blocks_use_decision_request_buttons():
    blocks = suggestion_blocks(_clarification_dict())
    actions = blocks[-2]["elements"]
    action_ids = [a["action_id"] for a in actions]

    assert action_ids == [
        "answer_clarification_option_0",
        "answer_clarification_option_1",
        "edit_suggestion",
        "approve_suggestion_clarification",
    ]
    assert len(action_ids) == len(set(action_ids))
    assert "approve_suggestion_clarification" in action_ids  # Ask Client
    assert "answer_clarification_internally" not in action_ids
    assert "edit_suggestion" in action_ids
    assert "reject_suggestion" not in action_ids

    labels = [a["text"]["text"] for a in actions]
    assert labels == ["A", "B", "Edit", "Ask Client"]
    assert "Ask Client" in labels


def test_clarification_blocks_render_structured_fields():
    blocks = suggestion_blocks(_clarification_dict())
    text = "\n".join(_section_texts(blocks))

    assert "Why it matters" in text
    assert "Known context" in text
    assert "Options" in text
    assert "Yes, read-only" in text
    assert "Recommended" in text


def test_clarification_answer_modal_prefills_recommended_option():
    modal = clarification_answer_modal(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "title": "Should cancelled users keep invoice access?",
            "payload": {"recommended_option": "Yes, read-only for 30 days"},
        }
    )

    assert modal["callback_id"] == "clarification_answer_modal"
    assert modal["blocks"][1]["block_id"] == "clarification_answer"
    element = modal["blocks"][1]["element"]
    assert element["action_id"] == "clarification_answer_input"
    assert element["initial_value"] == "Yes, read-only for 30 days"


def test_answer_internally_persists_marks_answered_and_stores_memory(monkeypatch):
    project = SimpleNamespace(id=uuid.uuid4(), internal_channel_id="C_INTERNAL")
    suggestion_id = uuid.uuid4()
    clarification_id = uuid.uuid4()

    events = {
        "marked": None,
        "memory": None,
        "channel": [],
        "dms": [],
    }

    async def claim(db, sid, user_id, client, body=None):
        return (
            SimpleNamespace(
                id=sid,
                project_id=project.id,
                title="What DB are we using?",
                body="Need to pick a store",
                payload={"submitter_slack_id": "U_DEV"},
                source_message_ids=[],
                source_refs=[],
            ),
            project,
        )

    async def insert_clarification(db, **kwargs):
        return SimpleNamespace(id=clarification_id, **kwargs)

    async def mark_answered(db, cid, final_answer, answered_by_slack_id=None):
        events["marked"] = (cid, final_answer, answered_by_slack_id)
        return SimpleNamespace(id=cid)

    async def embed_and_store(**kwargs):
        events["memory"] = kwargs

    async def send_channel_message(client, channel_id, text="", blocks=None):
        events["channel"].append((channel_id, text))

    async def send_dm(client, user_id, text="", blocks=None):
        events["dms"].append(user_id)

    async def sync(project_id, reason, client=None):
        return True

    monkeypatch.setattr(
        suggestion_approval_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(
        suggestion_approval_service, "can_approve_suggestion", lambda uid: True
    )
    monkeypatch.setattr(suggestion_approval_service, "_claim_suggestion", claim)
    monkeypatch.setattr(
        suggestion_approval_service, "insert_clarification", insert_clarification
    )
    monkeypatch.setattr(
        suggestion_approval_service, "mark_clarification_answered", mark_answered
    )
    monkeypatch.setattr(suggestion_approval_service, "embed_and_store", embed_and_store)
    monkeypatch.setattr(
        suggestion_approval_service, "send_channel_message", send_channel_message
    )
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    view = {
        "private_metadata": json.dumps({"suggestion_id": str(suggestion_id)}),
        "state": {
            "values": {
                "clarification_answer": {
                    "clarification_answer_input": {"value": "PostgreSQL 16"}
                }
            }
        },
    }
    body = {"user": {"id": "U_ADMIN"}}

    asyncio.run(
        suggestion_approval_service.handle_clarification_answer_submission(
            view, body, object()
        )
    )

    assert events["marked"][1] == "PostgreSQL 16"
    assert events["marked"][2] == "U_ADMIN"
    assert events["memory"]["source_type"] == "clarification"
    assert events["memory"]["source_id"] == clarification_id
    assert events["channel"] == [("C_INTERNAL", events["channel"][0][1])]
    # The original submitter is notified; the answering admin sees the source card update.
    assert "U_DEV" in events["dms"]
    assert "U_ADMIN" not in events["dms"]


def test_answer_clarification_option_persists_selected_option_and_updates_card(monkeypatch):
    project = SimpleNamespace(id=uuid.uuid4(), internal_channel_id="C_INTERNAL")
    suggestion_id = uuid.uuid4()
    clarification_id = uuid.uuid4()
    suggestion_payload = {
        "options": ["Yes, read-only", "No access"],
        "submitter_slack_id": "U_DEV",
    }
    events = {
        "marked": None,
        "memory": None,
        "updates": [],
        "dms": [],
        "channel": [],
    }

    async def claim(db, sid, user_id, client, body=None):
        return (
            SimpleNamespace(
                id=sid,
                project_id=project.id,
                title="Should cancelled users keep invoice access?",
                body="Need dashboard permission behavior",
                payload=suggestion_payload,
                source_message_ids=[],
                source_refs=[],
            ),
            project,
        )

    async def insert_clarification(db, **kwargs):
        return SimpleNamespace(id=clarification_id, **kwargs)

    async def mark_answered(db, cid, final_answer, answered_by_slack_id=None):
        events["marked"] = (cid, final_answer, answered_by_slack_id)

    async def embed_and_store(**kwargs):
        events["memory"] = kwargs

    async def send_channel_message(client, channel_id, text="", blocks=None):
        events["channel"].append((channel_id, text))

    async def send_dm(client, user_id, text="", blocks=None):
        events["dms"].append((user_id, text))

    async def update_message(client, channel_id, message_ts, text="", blocks=None):
        events["updates"].append((channel_id, message_ts, text, blocks))

    async def sync(project_id, reason, client=None):
        return True

    monkeypatch.setattr(
        suggestion_approval_service, "get_session_factory", lambda: lambda: FakeSession()
    )
    monkeypatch.setattr(
        suggestion_approval_service, "can_approve_suggestion", lambda uid: True
    )
    monkeypatch.setattr(suggestion_approval_service, "_claim_suggestion", claim)
    monkeypatch.setattr(
        suggestion_approval_service, "insert_clarification", insert_clarification
    )
    monkeypatch.setattr(
        suggestion_approval_service, "mark_clarification_answered", mark_answered
    )
    monkeypatch.setattr(suggestion_approval_service, "embed_and_store", embed_and_store)
    monkeypatch.setattr(
        suggestion_approval_service, "send_channel_message", send_channel_message
    )
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(suggestion_approval_service, "update_message", update_message)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    blocks = suggestion_blocks(
        {
            **_clarification_dict(),
            "id": str(suggestion_id),
            "payload": suggestion_payload,
        }
    )
    option_b = dict(blocks[-2]["elements"][1], block_id=blocks[-2]["block_id"])
    body = {
        "user": {"id": "U_ADMIN"},
        "container": {"channel_id": "D_ADMIN", "message_ts": "111.222"},
        "message": {"ts": "111.222", "blocks": blocks},
    }

    asyncio.run(
        suggestion_approval_service.answer_clarification_option(
            option_b, body, object()
        )
    )

    assert events["marked"] == (clarification_id, "No access", "U_ADMIN")
    assert events["memory"]["source_type"] == "clarification"
    assert events["updates"][0][0:2] == ("D_ADMIN", "111.222")
    updated_blocks = events["updates"][0][3]
    assert all(block.get("type") != "actions" for block in updated_blocks)
    assert "option B" in "\n".join(
        block["text"]["text"]
        for block in updated_blocks
        if block.get("type") == "section"
    )
    assert events["dms"] == [("U_DEV", "✅ Your clarification was answered: *Should cancelled users keep invoice access?*\nNo access")]


def test_dev_cannot_open_answer_modal(monkeypatch):
    denied = []
    opened = []

    async def deny(client, body, user_id):
        denied.append(user_id)

    async def open_modal(client, trigger_id, view):
        opened.append(view)

    monkeypatch.setattr(
        suggestion_approval_service, "can_approve_suggestion", lambda uid: False
    )
    monkeypatch.setattr(suggestion_approval_service, "_send_approval_denied", deny)
    monkeypatch.setattr(suggestion_approval_service, "open_modal", open_modal)

    action = {"value": json.dumps({"suggestion_id": str(uuid.uuid4())})}
    body = {"user": {"id": "U_DEV"}, "trigger_id": "T1"}

    asyncio.run(
        suggestion_approval_service.open_clarification_answer_modal(
            action, body, object()
        )
    )

    assert denied == ["U_DEV"]
    assert opened == []
