import asyncio
import uuid
from types import SimpleNamespace

from pm_agent.app.services import huddle_notes_service
from pm_agent.app.utils.priorities import clean_priority
from pm_agent.app.utils.slack_ids import looks_like_slack_user_id
from pm_agent.app.utils.text_cleaning import message_text_with_file_placeholders


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None


def test_file_share_placeholder_is_added_to_message_text():
    event = {
        "text": "Please use this spec",
        "files": [
            {
                "title": "Scope.pdf",
                "filetype": "pdf",
                "permalink": "https://slack.example/files/F1",
            }
        ],
    }

    text = message_text_with_file_placeholders(event)

    assert "Please use this spec" in text
    assert "[File shared: Scope.pdf (pdf) - https://slack.example/files/F1]" in text


def test_file_share_placeholder_works_without_message_text():
    event = {
        "files": [
            {
                "name": "wireframe.png",
                "mimetype": "image/png",
                "url_private": "https://slack.example/files/F2",
            }
        ],
    }

    assert message_text_with_file_placeholders(event) == (
        "[File shared: wireframe.png (image/png) - https://slack.example/files/F2]"
    )


def test_huddle_assignee_text_is_not_forced_to_owner_slack_id():
    assert looks_like_slack_user_id("U123ABC")
    assert looks_like_slack_user_id("W123ABC")
    assert not looks_like_slack_user_id("Ali")
    assert not looks_like_slack_user_id(None)


def test_huddle_priority_is_sanitized():
    assert clean_priority("urgent") == "urgent"
    assert clean_priority("critical") == "medium"
    assert clean_priority(None) == "medium"


def test_huddle_modal_requires_internal_project_channel(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
    )
    opened = []
    dms = []

    async def find_project(db, channel_id):
        return project

    async def open_modal(client, trigger_id, view):
        opened.append(view)

    async def send_dm(client, user_id, text="", blocks=None):
        dms.append((user_id, text))

    monkeypatch.setattr(huddle_notes_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(huddle_notes_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(huddle_notes_service, "open_modal", open_modal)
    monkeypatch.setattr(huddle_notes_service, "send_dm", send_dm)

    asyncio.run(
        huddle_notes_service.open_huddle_modal(
            object(), "TRIGGER", "C_CLIENT", user_id="U_DEV"
        )
    )

    assert opened == []
    assert dms == [("U_DEV", huddle_notes_service.HUDDLE_INTERNAL_ONLY)]


def test_huddle_processing_requires_internal_project_channel(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
    )
    dms = []

    async def find_project(db, channel_id):
        return project

    async def chain_should_not_run(*args, **kwargs):
        raise AssertionError("client-channel huddle notes should not run extraction")

    async def send_dm(client, user_id, text="", blocks=None):
        dms.append((user_id, text))

    monkeypatch.setattr(huddle_notes_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(huddle_notes_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(huddle_notes_service, "huddle_notes_chain", chain_should_not_run)
    monkeypatch.setattr(huddle_notes_service, "send_dm", send_dm)

    asyncio.run(
        huddle_notes_service._process_huddle_notes(
            "U_DEV", "C_CLIENT", "internal planning notes", None, object()
        )
    )

    assert dms == [("U_DEV", huddle_notes_service.HUDDLE_INTERNAL_ONLY)]
