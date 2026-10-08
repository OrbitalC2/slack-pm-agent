import asyncio
from types import SimpleNamespace

from pm_agent.app.api import slack_commands, slack_events
from pm_agent.app.services import project_service
from pm_agent.app.utils import permissions


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSlackApp:
    def __init__(self):
        self.handlers = {}

    def event(self, event_def):
        def decorator(fn):
            if event_def == "app_mention":
                self.handlers["app_mention"] = fn
            return fn

        return decorator


def test_client_channel_app_mention_exception_stays_silent(monkeypatch):
    async def boom(event, say, context):
        raise RuntimeError("db temporarily unavailable")

    async def silence(channel_id):
        return True

    from pm_agent.app.services import question_answer_service

    monkeypatch.setattr(question_answer_service, "handle_developer_question", boom)
    monkeypatch.setattr(question_answer_service, "should_silence_app_mention_error", silence)

    app = _FakeSlackApp()
    slack_events.register(app)
    replies = []

    async def say(**kwargs):
        replies.append(kwargs)

    asyncio.run(app.handlers["app_mention"]({"channel": "C_CLIENT", "ts": "1.0"}, say, {}))

    assert replies == []


def test_running_gather_error_reports_existing_channel_gather(monkeypatch):
    async def fake_find_project(db, channel_id):
        return SimpleNamespace(id="project-1", name="Project")

    async def fake_get_running(db, project_id, channel_id):
        return SimpleNamespace(id="gather-1")

    from pm_agent.app.db import gather_queries, project_queries, session

    monkeypatch.setattr(project_queries, "find_project_by_channel", fake_find_project)
    monkeypatch.setattr(gather_queries, "get_running_gather_for_channel", fake_get_running)
    monkeypatch.setattr(session, "get_session_factory", lambda: lambda: _FakeSession())

    message = asyncio.run(slack_commands._running_gather_error("C_INTERNAL"))

    assert "already in progress" in message


def test_new_project_setup_requires_global_admin(monkeypatch):
    async def no_existing(*args, **kwargs):
        return None

    async def create_should_not_run(*args, **kwargs):
        raise AssertionError("create_project should not run")

    monkeypatch.setattr(project_service, "_find_existing_project_for_setup", no_existing)
    monkeypatch.setattr(project_service, "is_global_admin", lambda user_id: False)
    monkeypatch.setattr(project_service, "create_project", create_should_not_run)

    project, updated, error = asyncio.run(
        project_service._save_project(
            db=object(),
            user_id="U_NOT_ADMIN",
            current_channel_id="C_CURRENT",
            name="Project",
            client_channel_id="C_CLIENT",
            client_name=None,
            internal_channel_id="C_INTERNAL",
            internal_channel_was_provided=True,
            github_url=None,
        )
    )

    assert project is None
    assert updated is False
    assert error == permissions.COMMAND_PERMISSION_DENIED


def test_existing_project_update_requires_global_admin(monkeypatch):
    existing = SimpleNamespace(id="project-1", internal_channel_id="C_INTERNAL")

    async def fake_existing(*args, **kwargs):
        return existing

    async def update_should_not_run(*args, **kwargs):
        raise AssertionError("update_project should not run")

    monkeypatch.setattr(project_service, "_find_existing_project_for_setup", fake_existing)
    monkeypatch.setattr(project_service, "is_global_admin", lambda user_id: False)
    monkeypatch.setattr(project_service, "update_project", update_should_not_run)

    project, updated, error = asyncio.run(
        project_service._save_project(
            db=object(),
            user_id="U_NOT_ADMIN",
            current_channel_id="C_INTERNAL",
            name="Project",
            client_channel_id="C_CLIENT",
            client_name=None,
            internal_channel_id=None,
            internal_channel_was_provided=False,
            github_url=None,
        )
    )

    assert project is None
    assert updated is False
    assert error == permissions.COMMAND_PERMISSION_DENIED
