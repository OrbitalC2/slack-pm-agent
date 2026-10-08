import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

from pm_agent.app.api import slack_events
from pm_agent.app.db import gather_queries
from pm_agent.app.db.models import (
    Base,
    Clarification,
    GatherRun,
    HuddleRun,
    Message,
    ProcessedEvent,
    Suggestion,
    Task,
)
from pm_agent.app.services import canvas_service, question_answer_service, suggestion_approval_service
from pm_agent.app.utils import permissions
from pm_agent.app.utils.slack_formatting import gather_summary_block_batches, suggestion_blocks


class _FakeSlackApp:
    def __init__(self):
        self.handlers = {}

    def event(self, event_def):
        def decorator(fn):
            if event_def == "message":
                self.handlers["message"] = fn
            elif event_def == "app_mention":
                self.handlers["app_mention"] = fn
            elif isinstance(event_def, dict):
                self.handlers[event_def["subtype"]] = fn
            return fn

        return decorator


class _FakeSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        self.commits += 1


def test_repeated_app_mention_event_id_is_deduped(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)
    seen = set()
    calls = []
    completed = []
    failed = []

    async def claim_once(event, context, body):
        event_id = slack_events._slack_event_id(event, context, body)
        if event_id in seen:
            return None
        seen.add(event_id)
        return event_id

    async def answer(event, say, context):
        calls.append(event["ts"])

    async def mark_completed(event_id):
        completed.append(event_id)

    async def mark_failed(event_id):
        failed.append(event_id)

    from pm_agent.app.services import question_answer_service as qa_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", claim_once)
    monkeypatch.setattr(slack_events, "_mark_slack_event_completed", mark_completed)
    monkeypatch.setattr(slack_events, "_mark_slack_event_failed", mark_failed)
    monkeypatch.setattr(qa_service, "handle_developer_question", answer)

    event = {"type": "app_mention", "channel": "C_INTERNAL", "ts": "1.0"}
    body = {"event_id": "Ev123"}

    asyncio.run(app.handlers["app_mention"](event, lambda **kwargs: None, {}, body))
    asyncio.run(app.handlers["app_mention"](event, lambda **kwargs: None, {}, body))

    assert calls == ["1.0"]
    assert completed == ["Ev123"]
    assert failed == []


def test_duplicate_completed_event_id_is_skipped(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)

    async def duplicate_completed(event, context, body):
        return None

    async def should_not_run(event, context):
        raise AssertionError("completed duplicate should be skipped")

    from pm_agent.app.services import message_ingest_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", duplicate_completed)
    monkeypatch.setattr(message_ingest_service, "ingest_message", should_not_run)

    asyncio.run(
        app.handlers["message"](
            {"type": "message", "channel": "C", "ts": "1.0", "text": "original"},
            None,
            {},
            {"event_id": "EvCompleted"},
        )
    )


def test_duplicate_processing_event_id_is_skipped(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)

    async def duplicate_processing(event, context, body):
        return None

    async def should_not_run(event, say, context):
        raise AssertionError("processing duplicate should be skipped")

    from pm_agent.app.services import question_answer_service as qa_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", duplicate_processing)
    monkeypatch.setattr(qa_service, "handle_developer_question", should_not_run)

    asyncio.run(
        app.handlers["app_mention"](
            {"type": "app_mention", "channel": "C_INTERNAL", "ts": "1.0"},
            lambda **kwargs: None,
            {},
            {"event_id": "EvProcessing"},
        )
    )


def test_failed_handler_marks_event_failed_not_completed(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)
    completed = []
    failed = []

    async def claim(event, context, body):
        return slack_events._slack_event_id(event, context, body)

    async def fail_ingest(event, context):
        raise RuntimeError("temporary db outage")

    async def mark_completed(event_id):
        completed.append(event_id)

    async def mark_failed(event_id):
        failed.append(event_id)

    from pm_agent.app.services import message_ingest_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", claim)
    monkeypatch.setattr(slack_events, "_mark_slack_event_completed", mark_completed)
    monkeypatch.setattr(slack_events, "_mark_slack_event_failed", mark_failed)
    monkeypatch.setattr(message_ingest_service, "ingest_message", fail_ingest)

    asyncio.run(
        app.handlers["message"](
            {"type": "message", "channel": "C", "ts": "1.0", "text": "original"},
            None,
            {},
            {"event_id": "EvFailed"},
        )
    )

    assert completed == []
    assert failed == ["EvFailed"]


def test_retry_after_failure_can_process_successfully(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)
    statuses = {}
    calls = {"count": 0}

    async def stateful_claim(event, context, body):
        event_id = slack_events._slack_event_id(event, context, body)
        if statuses.get(event_id) in {"processing", "completed"}:
            return None
        statuses[event_id] = "processing"
        return event_id

    async def mark_completed(event_id):
        statuses[event_id] = "completed"

    async def mark_failed(event_id):
        statuses[event_id] = "failed"

    async def flaky_ingest(event, context):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("first attempt fails")

    from pm_agent.app.services import message_ingest_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", stateful_claim)
    monkeypatch.setattr(slack_events, "_mark_slack_event_completed", mark_completed)
    monkeypatch.setattr(slack_events, "_mark_slack_event_failed", mark_failed)
    monkeypatch.setattr(message_ingest_service, "ingest_message", flaky_ingest)

    event = {"type": "message", "channel": "C", "ts": "1.0", "text": "original"}
    body = {"event_id": "EvRetry"}

    asyncio.run(app.handlers["message"](event, None, {}, body))
    asyncio.run(app.handlers["message"](event, None, {}, body))

    assert calls["count"] == 2
    assert statuses["EvRetry"] == "completed"


def test_distinct_message_edit_event_id_is_not_blocked_by_original_message(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)
    seen = set()
    ingested = []

    async def claim_once(event, context, body):
        event_id = slack_events._slack_event_id(event, context, body)
        if event_id in seen:
            return None
        seen.add(event_id)
        return event_id

    async def ingest(event, context):
        ingested.append(event.get("subtype") or "message")

    async def noop(event_id):
        return None

    from pm_agent.app.services import message_ingest_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", claim_once)
    monkeypatch.setattr(slack_events, "_mark_slack_event_completed", noop)
    monkeypatch.setattr(slack_events, "_mark_slack_event_failed", noop)
    monkeypatch.setattr(message_ingest_service, "ingest_message", ingest)

    asyncio.run(
        app.handlers["message"](
            {"type": "message", "channel": "C", "ts": "1.0", "text": "original"},
            None,
            {},
            {"event_id": "EvOriginal"},
        )
    )
    asyncio.run(
        app.handlers["message_changed"](
            {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C",
                "message": {"ts": "1.0", "text": "edited"},
            },
            {},
            {"event_id": "EvEdit"},
        )
    )

    assert ingested == ["message", "message_changed"]


def test_distinct_message_delete_event_id_is_not_blocked_by_original_message(monkeypatch):
    app = _FakeSlackApp()
    slack_events.register(app)
    seen = set()
    ingested = []

    async def claim_once(event, context, body):
        event_id = slack_events._slack_event_id(event, context, body)
        if event_id in seen:
            return None
        seen.add(event_id)
        return event_id

    async def ingest(event, context):
        ingested.append(event.get("subtype") or "message")

    async def noop(event_id):
        return None

    from pm_agent.app.services import message_ingest_service

    monkeypatch.setattr(slack_events, "_claim_slack_event", claim_once)
    monkeypatch.setattr(slack_events, "_mark_slack_event_completed", noop)
    monkeypatch.setattr(slack_events, "_mark_slack_event_failed", noop)
    monkeypatch.setattr(message_ingest_service, "ingest_message", ingest)

    asyncio.run(
        app.handlers["message"](
            {"type": "message", "channel": "C", "ts": "1.0", "text": "original"},
            None,
            {},
            {"event_id": "EvOriginal"},
        )
    )
    asyncio.run(
        app.handlers["message_deleted"](
            {
                "type": "message",
                "subtype": "message_deleted",
                "channel": "C",
                "deleted_ts": "1.0",
            },
            {},
            {"event_id": "EvDelete"},
        )
    )

    assert ingested == ["message", "message_deleted"]


def test_gather_pointer_lookup_is_scoped_by_project_and_channel():
    project_id = uuid.uuid4()

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class FakeDB:
        def __init__(self):
            self.channels = []

        async def execute(self, statement):
            params = statement.compile().params
            channel = next(
                value for key, value in params.items() if "slack_channel_id" in key
            )
            self.channels.append(channel)
            return Result(SimpleNamespace(to_slack_ts=f"ts-{channel}"))

    db = FakeDB()

    client_pointer = asyncio.run(
        gather_queries.get_latest_completed_gather_for_channel(db, project_id, "C_CLIENT")
    )
    internal_pointer = asyncio.run(
        gather_queries.get_latest_completed_gather_for_channel(db, project_id, "C_INTERNAL")
    )

    assert client_pointer.to_slack_ts == "ts-C_CLIENT"
    assert internal_pointer.to_slack_ts == "ts-C_INTERNAL"
    assert db.channels == ["C_CLIENT", "C_INTERNAL"]


def test_approval_retry_creates_only_one_task(monkeypatch):
    suggestion_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suggestion = SimpleNamespace(
        id=suggestion_id,
        project_id=project_id,
        title="Build export",
        body="Add CSV export",
        payload={"priority": "high"},
        source_message_ids=["m1"],
        source_refs=[],
    )
    project = SimpleNamespace(id=project_id, internal_channel_id=None, name="Project")
    claim_count = {"count": 0}
    tasks = []

    async def get_suggestion(db, sid):
        return suggestion

    async def get_project(db, pid):
        return project

    async def claim(db, sid, user_id):
        claim_count["count"] += 1
        return suggestion if claim_count["count"] == 1 else None

    async def insert_task(*args, **kwargs):
        tasks.append(kwargs)
        return SimpleNamespace(title=kwargs["title"], description=kwargs["description"], owner_slack_id=None, priority=kwargs["priority"])

    async def send_dm(*args, **kwargs):
        return None

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_suggestion_by_id", get_suggestion)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "claim_pending_suggestion", claim)
    monkeypatch.setattr(suggestion_approval_service, "insert_task", insert_task)
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(suggestion_approval_service, "can_approve_suggestion", lambda user_id: True)

    action = {"value": f'{{"suggestion_id":"{suggestion_id}"}}'}
    body = {"user": {"id": "U_APPROVER"}}

    asyncio.run(suggestion_approval_service.approve_task_suggestion(action, body, object()))
    asyncio.run(suggestion_approval_service.approve_task_suggestion(action, body, object()))

    assert len(tasks) == 1
    assert tasks[0]["source_suggestion_id"] == suggestion_id


def test_task_approval_updates_source_card_without_success_dm(monkeypatch):
    suggestion_id = uuid.uuid4()
    project_id = uuid.uuid4()
    gather_run_id = uuid.uuid4()
    suggestion = SimpleNamespace(
        id=suggestion_id,
        project_id=project_id,
        title="Build export",
        body="Add CSV export",
        payload={"priority": "high"},
        source_message_ids=["m1"],
        source_refs=[],
    )
    project = SimpleNamespace(id=project_id, internal_channel_id=None, name="Project")
    dms = []
    updates = []

    async def get_suggestion(db, sid):
        return suggestion

    async def get_project(db, pid):
        return project

    async def claim(db, sid, user_id):
        return suggestion

    async def insert_task(*args, **kwargs):
        return SimpleNamespace(
            title=kwargs["title"],
            description=kwargs["description"],
            owner_slack_id=None,
            priority=kwargs["priority"],
        )

    async def send_dm(*args, **kwargs):
        dms.append(kwargs.get("text"))

    async def update_msg(client, channel_id, message_ts, text="", blocks=None):
        updates.append((channel_id, message_ts, text, blocks))

    async def sync(project_id, reason, client=None):
        return True

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_suggestion_by_id", get_suggestion)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "claim_pending_suggestion", claim)
    monkeypatch.setattr(suggestion_approval_service, "insert_task", insert_task)
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(suggestion_approval_service, "update_message", update_msg)
    monkeypatch.setattr(suggestion_approval_service, "can_approve_suggestion", lambda user_id: True)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    blocks = suggestion_blocks(
        {
            "id": str(suggestion_id),
            "suggestion_type": "task",
            "title": suggestion.title,
            "body": suggestion.body,
            "source_message_ids": suggestion.source_message_ids,
            "source_refs": suggestion.source_refs,
            "payload": suggestion.payload,
            "gather_run_id": str(gather_run_id),
        }
    )
    action = dict(blocks[-2]["elements"][0], block_id=blocks[-2]["block_id"])
    body = {
        "user": {"id": "U_APPROVER"},
        "container": {"channel_id": "D_ADMIN", "message_ts": "123.456"},
        "message": {"ts": "123.456", "blocks": blocks},
    }

    asyncio.run(suggestion_approval_service.approve_task_suggestion(action, body, object()))

    assert dms == []
    assert len(updates) == 1
    assert updates[0][0:2] == ("D_ADMIN", "123.456")
    updated_blocks = updates[0][3]
    assert all(block.get("type") != "actions" for block in updated_blocks)
    assert "Approved" in "\n".join(
        block["text"]["text"]
        for block in updated_blocks
        if block.get("type") == "section"
    )


def test_approve_all_updates_every_task_action_row_in_clicked_message(monkeypatch):
    gather_run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suggestions = [
        SimpleNamespace(
            id=uuid.uuid4(),
            project_id=project_id,
            title="Task A",
            body="Build A",
            payload={"priority": "high"},
            source_message_ids=[],
            source_refs=[],
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            project_id=project_id,
            title="Task B",
            body="Build B",
            payload={"priority": "medium"},
            source_message_ids=[],
            source_refs=[],
        ),
    ]
    project = SimpleNamespace(id=project_id, internal_channel_id=None, name="Project")
    updates = []
    dms = []

    async def get_pending(db, gid):
        assert gid == gather_run_id
        return suggestions

    async def get_project(db, pid):
        return project

    async def claim(db, sid, user_id):
        return next((s for s in suggestions if s.id == sid), None)

    async def insert_task(*args, **kwargs):
        return SimpleNamespace(
            title=kwargs["title"],
            description=kwargs["description"],
            owner_slack_id=None,
            priority=kwargs["priority"],
        )

    async def update_msg(client, channel_id, message_ts, text="", blocks=None):
        updates.append((channel_id, message_ts, text, blocks))

    async def send_dm(*args, **kwargs):
        dms.append(kwargs.get("text"))

    async def sync(project_id, reason, client=None):
        return True

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_pending_tasks_for_gather_run", get_pending)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "claim_pending_suggestion", claim)
    monkeypatch.setattr(suggestion_approval_service, "insert_task", insert_task)
    monkeypatch.setattr(suggestion_approval_service, "update_message", update_msg)
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(suggestion_approval_service, "can_approve_suggestion", lambda user_id: True)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    batches = gather_summary_block_batches(
        "Project",
        "Summary",
        [],
        [
            {
                "id": str(s.id),
                "suggestion_type": "task",
                "title": s.title,
                "body": s.body,
                "source_message_ids": [],
                "source_refs": [],
                "payload": s.payload,
                "gather_run_id": str(gather_run_id),
            }
            for s in suggestions
        ],
        str(gather_run_id),
    )
    task_batch = next(
        batch
        for batch in batches
        if any(
            element.get("action_id") == "approve_all_tasks_from_gather"
            for block in batch
            if block.get("type") == "actions"
            for element in block.get("elements", [])
        )
    )
    approve_all_block = next(
        block
        for block in task_batch
        if block.get("type") == "actions"
        and block["elements"][0]["action_id"] == "approve_all_tasks_from_gather"
    )
    assert sum(
        1
        for block in task_batch
        if block.get("type") == "actions"
        and any(e.get("action_id") == "approve_suggestion_task" for e in block["elements"])
    ) == 2

    action = dict(approve_all_block["elements"][0], block_id=approve_all_block["block_id"])
    body = {
        "user": {"id": "U_APPROVER"},
        "container": {"channel_id": "D_ADMIN", "message_ts": "123.456"},
        "message": {"ts": "123.456", "blocks": task_batch},
    }

    asyncio.run(suggestion_approval_service.approve_all_tasks(action, body, object()))

    assert dms == []
    assert len(updates) == 1
    updated_blocks = updates[0][3]
    assert all(
        not any(e.get("action_id") == "approve_suggestion_task" for e in block.get("elements", []))
        for block in updated_blocks
        if block.get("type") == "actions"
    )
    status_text = "\n".join(
        block["text"]["text"]
        for block in updated_blocks
        if block.get("type") == "section"
    )
    assert "Approved 2 tasks" in status_text
    assert status_text.count("*Approved* by <@U_APPROVER>") == 2


def test_edit_submission_still_saves_user_content(monkeypatch):
    suggestion_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suggestion = SimpleNamespace(
        id=suggestion_id,
        project_id=project_id,
        status="pending",
        payload={"priority": "low"},
    )
    saved = {}

    async def get_suggestion(db, sid):
        return suggestion

    async def get_project(db, pid):
        return SimpleNamespace(id=project_id)

    async def update_content(db, sid, title=None, body=None, payload=None):
        saved.update({"sid": sid, "title": title, "body": body, "payload": payload})
        return suggestion

    async def send_dm(*args, **kwargs):
        return None

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_suggestion_by_id", get_suggestion)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "update_suggestion_content", update_content)
    monkeypatch.setattr(suggestion_approval_service, "send_dm", send_dm)
    monkeypatch.setattr(suggestion_approval_service, "can_approve_suggestion", lambda user_id: True)

    view = {
        "private_metadata": f'{{"suggestion_id":"{suggestion_id}"}}',
        "state": {
            "values": {
                "edit_title": {"edit_title_input": {"value": "Edited title"}},
                "edit_body": {"edit_body_input": {"value": "Edited body"}},
                "edit_priority": {
                    "edit_priority_input": {
                        "selected_option": {"value": "urgent"},
                    }
                },
            }
        },
    }
    body = {"user": {"id": "U_APPROVER"}}

    asyncio.run(suggestion_approval_service.handle_edit_modal_submission(view, body, object()))

    assert saved == {
        "sid": suggestion_id,
        "title": "Edited title",
        "body": "Edited body",
        "payload": {"priority": "urgent"},
    }


def test_dev_clicking_approve_task_is_blocked(monkeypatch):
    suggestion_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suggestion = SimpleNamespace(id=suggestion_id, project_id=project_id)
    ephemerals = []

    async def get_suggestion(db, sid):
        raise AssertionError("dev edit should not look up the suggestion")

    async def get_project(db, pid):
        return SimpleNamespace(id=project_id)

    async def claim_should_not_run(*args, **kwargs):
        raise AssertionError("dev approval should not claim suggestion")

    async def send_ephemeral(client, channel_id, user_id, text):
        ephemerals.append((channel_id, user_id, text))

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_suggestion_by_id", get_suggestion)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "claim_pending_suggestion", claim_should_not_run)
    monkeypatch.setattr(suggestion_approval_service, "send_ephemeral", send_ephemeral, raising=False)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))

    action = {"value": f'{{"suggestion_id":"{suggestion_id}"}}'}
    body = {"user": {"id": "U_DEV"}, "channel": {"id": "C_INTERNAL"}}

    asyncio.run(suggestion_approval_service.approve_task_suggestion(action, body, object()))

    assert ephemerals == [
        ("C_INTERNAL", "U_DEV", permissions.APPROVAL_PERMISSION_DENIED)
    ]


def test_dev_clicking_reject_is_blocked(monkeypatch):
    suggestion_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suggestion = SimpleNamespace(id=suggestion_id, project_id=project_id)
    ephemerals = []

    async def get_suggestion(db, sid):
        raise AssertionError("dev reject should not look up the suggestion")

    async def get_project(db, pid):
        return SimpleNamespace(id=project_id)

    async def update_should_not_run(*args, **kwargs):
        raise AssertionError("dev reject should not update suggestion")

    async def send_ephemeral(client, channel_id, user_id, text):
        ephemerals.append((channel_id, user_id, text))

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_suggestion_by_id", get_suggestion)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "update_suggestion_status", update_should_not_run)
    monkeypatch.setattr(suggestion_approval_service, "send_ephemeral", send_ephemeral, raising=False)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))

    action = {"value": f'{{"suggestion_id":"{suggestion_id}"}}'}
    body = {"user": {"id": "U_DEV"}, "channel": {"id": "C_INTERNAL"}}

    asyncio.run(suggestion_approval_service.reject_suggestion(action, body, object()))

    assert ephemerals == [
        ("C_INTERNAL", "U_DEV", permissions.APPROVAL_PERMISSION_DENIED)
    ]


def test_dev_clicking_edit_is_blocked(monkeypatch):
    suggestion_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suggestion = SimpleNamespace(id=suggestion_id, project_id=project_id)
    ephemerals = []

    async def get_suggestion(db, sid):
        raise AssertionError("dev edit should not look up the suggestion")

    async def get_project(db, pid):
        return SimpleNamespace(id=project_id)

    async def open_should_not_run(*args, **kwargs):
        raise AssertionError("dev edit should not open modal")

    async def send_ephemeral(client, channel_id, user_id, text):
        ephemerals.append((channel_id, user_id, text))

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_suggestion_by_id", get_suggestion)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "open_modal", open_should_not_run, raising=False)
    monkeypatch.setattr(suggestion_approval_service, "send_ephemeral", send_ephemeral, raising=False)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))

    action = {"value": f'{{"suggestion_id":"{suggestion_id}"}}'}
    body = {
        "user": {"id": "U_DEV"},
        "channel": {"id": "C_INTERNAL"},
        "trigger_id": "TRIGGER",
    }

    asyncio.run(suggestion_approval_service.open_edit_modal(action, body, object()))

    assert ephemerals == [
        ("C_INTERNAL", "U_DEV", permissions.APPROVAL_PERMISSION_DENIED)
    ]


def test_dev_clicking_approve_all_is_blocked(monkeypatch):
    gather_run_id = uuid.uuid4()
    project_id = uuid.uuid4()
    pending = [SimpleNamespace(id=uuid.uuid4(), project_id=project_id)]
    ephemerals = []

    async def get_pending(db, gid):
        raise AssertionError("dev approve-all should not look up pending suggestions")

    async def get_project(db, pid):
        return SimpleNamespace(id=project_id)

    async def claim_should_not_run(*args, **kwargs):
        raise AssertionError("dev approve-all should not claim suggestions")

    async def send_ephemeral(client, channel_id, user_id, text):
        ephemerals.append((channel_id, user_id, text))

    monkeypatch.setattr(suggestion_approval_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(suggestion_approval_service, "get_pending_tasks_for_gather_run", get_pending)
    monkeypatch.setattr(suggestion_approval_service, "get_project_by_id", get_project)
    monkeypatch.setattr(suggestion_approval_service, "claim_pending_suggestion", claim_should_not_run)
    monkeypatch.setattr(suggestion_approval_service, "send_ephemeral", send_ephemeral, raising=False)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))

    action = {"value": f'{{"gather_run_id":"{gather_run_id}"}}'}
    body = {"user": {"id": "U_DEV"}, "channel": {"id": "C_INTERNAL"}}

    asyncio.run(suggestion_approval_service.approve_all_tasks(action, body, object()))

    assert ephemerals == [
        ("C_INTERNAL", "U_DEV", permissions.APPROVAL_PERMISSION_DENIED)
    ]


def test_qa_memory_context_expands_source_refs():
    source_id = uuid.uuid4()
    source = SimpleNamespace(
        source_refs=[
            {
                "type": "slack_message",
                "message_id": "m1",
                "channel_id": "C_INTERNAL",
                "slack_message_ts": "123.456",
                "permalink": "https://slack.example/archives/C_INTERNAL/p123456",
            }
        ],
        source_message_ids=[],
    )

    class FakeDB:
        async def get(self, model, item_id):
            assert model is Suggestion
            assert item_id == source_id
            return source

    lines = asyncio.run(
        question_answer_service._memory_context_lines(
            FakeDB(),
            [
                {
                    "source_type": "decision",
                    "source_id": str(source_id),
                    "text": "Approved decision: Use Stripe",
                }
            ],
        )
    )

    assert "Approved decision: Use Stripe" in lines[0]
    assert "https://slack.example/archives/C_INTERNAL/p123456" in lines[0]


def test_client_channel_slash_ask_still_blocks_before_retrieval(monkeypatch):
    project = SimpleNamespace(client_channel_id="C_CLIENT", internal_channel_id="C_INTERNAL")

    async def find_project(db, channel_id):
        return project

    async def retrieve_should_not_run(*args, **kwargs):
        raise AssertionError("client-channel ask should not retrieve memory")

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: _FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", retrieve_should_not_run)

    result = asyncio.run(question_answer_service.handle_slash_ask("U_CLIENT", "C_CLIENT", "what changed?"))

    assert "only available in the internal project channel" in result


def test_schema_metadata_and_migrations_include_critical_p2_columns_and_indexes():
    assert "slack_channel_id" in GatherRun.__table__.c
    assert "notification_status" in GatherRun.__table__.c
    assert "source_refs" in Suggestion.__table__.c
    assert "source_refs" in Task.__table__.c
    assert "source_refs" in Clarification.__table__.c
    assert "is_deleted" in Message.__table__.c
    assert "updated_at" in Message.__table__.c
    assert "status" in ProcessedEvent.__table__.c
    assert HuddleRun.__tablename__ in Base.metadata.tables

    versions_dir = Path("pm_agent/app/db/migrations/versions")
    phase1 = (versions_dir / "002_phase1_channel_gather_and_approval_safety.py").read_text()
    phase2 = (versions_dir / "003_phase2_source_tracking_and_huddle_provenance.py").read_text()
    phase3 = (versions_dir / "004_phase3_setup_and_schema_cleanup.py").read_text()
    p1_guard = (versions_dir / "005_p1_gather_running_guard.py").read_text()
    event_states = (versions_dir / "006_processed_event_states.py").read_text()

    assert "uq_tasks_source_suggestion_id" in phase1
    assert "uq_clarifications_source_suggestion_id" in phase1
    assert "source_refs" in phase2
    assert "huddle_runs" in phase2
    assert "is_deleted" in phase2
    assert "updated_at" in phase2
    assert "uq_projects_internal_channel_id" in phase3
    assert "uq_gather_runs_running_project_channel" in p1_guard
    assert "status = 'running'" in p1_guard
    assert "ck_processed_event_status" in event_states
    assert "processing" in event_states
    assert "completed" in event_states
    assert "failed" in event_states
