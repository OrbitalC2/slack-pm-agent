import asyncio
from types import SimpleNamespace

from pm_agent.app.api import slack_commands
from pm_agent.app.utils import permissions
from pm_agent.app.utils.permissions import (
    get_project_channel_kind,
    is_internal_project_channel,
)


def test_project_channel_kind_distinguishes_client_and_internal():
    project = SimpleNamespace(client_channel_id="C_CLIENT", internal_channel_id="C_INTERNAL")

    assert get_project_channel_kind(project, "C_CLIENT") == "client"
    assert get_project_channel_kind(project, "C_INTERNAL") == "internal"
    assert get_project_channel_kind(project, "C_OTHER") is None


def test_internal_project_channel_requires_internal_match():
    project = SimpleNamespace(client_channel_id="C_CLIENT", internal_channel_id="C_INTERNAL")

    assert is_internal_project_channel(project, "C_INTERNAL")
    assert not is_internal_project_channel(project, "C_CLIENT")


def test_global_admin_role_and_command_permissions(monkeypatch):
    monkeypatch.setattr(
        permissions,
        "get_settings",
        lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN", "U_OTHER"]),
    )

    assert permissions.is_global_admin(" U_ADMIN ")
    assert permissions.get_user_role("U_ADMIN") == "admin"
    assert permissions.get_user_role("U_DEV") == "dev"

    for command_name in {
        "setup-project",
        "gather-info",
        "huddle-notes",
        "update",
        "status",
        "blockers",
        "ask",
        "help",
    }:
        assert permissions.can_run_command("U_ADMIN", command_name)

    assert permissions.can_approve_suggestion("U_ADMIN")


def test_empty_global_admins_make_everyone_dev(monkeypatch):
    monkeypatch.setattr(
        permissions,
        "get_settings",
        lambda: SimpleNamespace(admin_slack_ids=[]),
    )

    assert not permissions.is_global_admin("U_ANYONE")
    assert permissions.get_user_role("U_ANYONE") == "dev"
    assert not permissions.can_approve_suggestion("U_ANYONE")


def test_dev_command_permissions(monkeypatch):
    monkeypatch.setattr(
        permissions,
        "get_settings",
        lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]),
    )

    for command_name in {"status", "blockers", "ask", "huddle-notes", "help"}:
        assert permissions.can_run_command("U_DEV", command_name)

    for command_name in {"setup-project", "gather-info", "update"}:
        assert not permissions.can_run_command("U_DEV", command_name)


def test_project_approver_wrapper_uses_global_admin_only(monkeypatch):
    monkeypatch.setattr(
        permissions,
        "get_settings",
        lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]),
    )
    project = SimpleNamespace(project_manager_slack_id="U_MANAGER")

    assert not permissions.is_project_approver("U_MANAGER", project)
    assert permissions.is_project_approver("U_ADMIN", project)


def test_help_text_is_role_specific(monkeypatch):
    monkeypatch.setattr(
        permissions,
        "get_settings",
        lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]),
    )

    admin_help = slack_commands._help_text("U_ADMIN")
    dev_help = slack_commands._help_text("U_DEV")

    assert "`/agent setup-project` - Create or update a project mapping" in admin_help
    assert "`/agent gather-info` - Analyze recent channel messages and create suggestions" in admin_help
    assert "`/agent update` - Generate a private client-ready update draft" in admin_help
    assert "`/agent ask <question>` - Ask project questions with full admin access" in admin_help

    assert "`/agent status` - Show project status" in dev_help
    assert "`/agent huddle-notes` - Submit huddle notes for processing" in dev_help
    assert "`/agent ask <question>` - Ask implementation/project-context questions" in dev_help
    assert "setup-project" not in dev_help
    assert "gather-info" not in dev_help
    assert "Generate a private client-ready update draft" not in dev_help
    assert "full admin access" not in dev_help


def test_dev_admin_only_slash_command_is_blocked_privately(monkeypatch):
    monkeypatch.setattr(
        permissions,
        "get_settings",
        lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]),
    )

    app = _FakeSlashApp()
    slack_commands.register(app)
    responses = []

    async def ack():
        return None

    async def respond(text):
        responses.append(text)

    async def should_not_run(*args, **kwargs):
        raise AssertionError("admin-only command handler should not run for dev users")

    monkeypatch.setattr(slack_commands, "_handle_gather_info", should_not_run)

    asyncio.run(
        app.handlers["/agent"](
            ack=ack,
            command={"text": "gather-info", "user_id": "U_DEV", "channel_id": "C_INTERNAL"},
            say=None,
            respond=respond,
            client=object(),
            context={},
        )
    )

    assert responses == [
        "You do not have permission to run this command. Ask an admin to run it."
    ]


class _FakeSlashApp:
    def __init__(self):
        self.handlers = {}

    def command(self, command_name):
        def decorator(fn):
            self.handlers[command_name] = fn
            return fn

        return decorator
