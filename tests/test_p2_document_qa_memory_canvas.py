"""Tests for P2 document/canvas improvements:

1. Q&A document awareness (internal channel only, no client leakage).
2. Memory creation from completed document analysis (idempotent, embedding fallback).
3. Best-effort Canvas sync after document processing.
4. Explicit skipped/partial analysis summary.
"""

import asyncio
import uuid
from types import SimpleNamespace

from pm_agent.app.agents.schemas import AnswerQuestionOutput
from pm_agent.app.services import (
    canvas_service,
    document_service,
    memory_service,
    question_answer_service,
)
from pm_agent.app.utils import permissions


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None


def _completed_document(**overrides):
    values = {
        "id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "slack_file_id": "F1",
        "title": None,
        "filename": "spec.pdf",
        "analysis_status": "completed",
        "analysis_summary": "Customer portal with billing.",
        "analysis_items": {
            "requirements": [{"text": "Users can download invoices"}],
            "clarifications": [{"question": "Can cancelled users download invoices?"}],
            "risks": [{"text": "Tight billing timeline"}],
            "decisions": [{"text": "Use Stripe"}],
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ── Fix #1: Q&A document awareness ────────────────────────────────────────────


def test_document_context_lines_format_and_cap():
    results = [
        {
            "text": "Invoices must be downloadable as PDF.",
            "document_id": "d1",
            "filename": "requirements.pdf",
            "title": None,
            "page_number": 3,
            "chunk_index": 7,
            "permalink": "https://slack.example/files/F1",
        },
        {
            "text": "X" * 5000,
            "document_id": "d2",
            "filename": "huge.pdf",
            "title": "Huge Spec",
            "page_number": None,
            "chunk_index": 0,
            "permalink": None,
        },
    ]

    lines = question_answer_service._document_context_lines(results)

    assert "requirements.pdf (p.3, chunk 7)" in lines[0]
    assert "https://slack.example/files/F1" in lines[0]
    assert "Invoices must be downloadable" in lines[0]
    # Per-chunk char cap is applied.
    assert all(
        len(segment) <= question_answer_service.MAX_QA_DOCUMENT_CHUNK_CHARS + 1
        for line in lines
        for segment in [line.split("\n  ", 1)[-1]]
    )


def test_qa_uses_document_chunks_in_internal_channel(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )
    captured = {}

    async def find_project(db, channel_id):
        return project

    async def empty(*args, **kwargs):
        return []

    async def no_memory(*args, **kwargs):
        return []

    async def document_chunks(project_id, question, limit=6):
        return [
            {
                "text": "The portal must support invoice PDF export.",
                "document_id": "d1",
                "filename": "requirements.pdf",
                "title": None,
                "page_number": 2,
                "chunk_index": 4,
                "permalink": "https://slack.example/files/F1",
            }
        ]

    async def answer_chain(question, context, llm_client):
        captured["context"] = context
        return AnswerQuestionOutput(answer="Yes, PDF export is required.", sources=[])

    async def classify_allowed(question, llm_client):
        return SimpleNamespace(classification="allowed_dev_query")

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "get_tasks", empty)
    monkeypatch.setattr(question_answer_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(question_answer_service, "get_approved_suggestions", empty)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", no_memory)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", document_chunks)
    monkeypatch.setattr(question_answer_service, "answer_question_chain", answer_chain)
    monkeypatch.setattr(question_answer_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=[]))
    monkeypatch.setattr(permissions, "get_llm_client", lambda: object())
    monkeypatch.setattr(permissions, "classify_dev_ask_chain", classify_allowed, raising=False)

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_DEV", "C_INTERNAL", "Do we need PDF export?")
    )

    assert "PDF export is required" in result
    assert "## Relevant Document Excerpts" in captured["context"]
    assert "requirements.pdf" in captured["context"]
    assert "invoice PDF export" in captured["context"]
    assert "https://slack.example/files/F1" in captured["context"]


def test_client_channel_ask_never_retrieves_documents(monkeypatch):
    project = SimpleNamespace(client_channel_id="C_CLIENT", internal_channel_id="C_INTERNAL")

    async def find_project(db, channel_id):
        return project

    async def documents_should_not_run(*args, **kwargs):
        raise AssertionError("client-channel ask must not retrieve document chunks")

    async def memory_should_not_run(*args, **kwargs):
        raise AssertionError("client-channel ask must not retrieve memory")

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", memory_should_not_run)
    monkeypatch.setattr(
        question_answer_service, "retrieve_relevant_document_chunks", documents_should_not_run
    )

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_CLIENT", "C_CLIENT", "what changed?")
    )

    assert "only available in the internal project channel" in result


def test_dev_ask_blocks_budget_query_before_retrieval(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="Should not be read",
    )

    async def find_project(db, channel_id):
        return project

    async def should_not_retrieve(*args, **kwargs):
        raise AssertionError("restricted dev ask should not retrieve project context")

    async def should_not_classify(*args, **kwargs):
        raise AssertionError("keyword-restricted dev ask should not call the classifier")

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "get_tasks", should_not_retrieve)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", should_not_retrieve)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", should_not_retrieve)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))
    monkeypatch.setattr(permissions, "classify_dev_ask_chain", should_not_classify, raising=False)

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_DEV", "C_INTERNAL", "What is the project budget?")
    )

    assert result == permissions.DEV_ASK_RESTRICTED_MESSAGE


def test_dev_ask_blocks_client_reply_draft_before_retrieval(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="Should not be read",
    )

    async def find_project(db, channel_id):
        return project

    async def should_not_retrieve(*args, **kwargs):
        raise AssertionError("restricted dev ask should not retrieve project context")

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "get_tasks", should_not_retrieve)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", should_not_retrieve)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", should_not_retrieve)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_DEV", "C_INTERNAL", "Draft a client reply.")
    )

    assert result == permissions.DEV_ASK_RESTRICTED_MESSAGE


def test_dev_ask_allows_internal_task_deadline_query(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )

    async def find_project(db, channel_id):
        return project

    async def empty(*args, **kwargs):
        return []

    async def classify_allowed(question, llm_client):
        return SimpleNamespace(classification="allowed_dev_query")

    async def answer_chain(question, context, llm_client):
        return AnswerQuestionOutput(answer="The urgent task is invoice download.", sources=[])

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "get_tasks", empty)
    monkeypatch.setattr(question_answer_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(question_answer_service, "get_approved_suggestions", empty)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", empty)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", empty)
    monkeypatch.setattr(question_answer_service, "answer_question_chain", answer_chain)
    monkeypatch.setattr(question_answer_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))
    monkeypatch.setattr(permissions, "get_llm_client", lambda: object())
    monkeypatch.setattr(permissions, "classify_dev_ask_chain", classify_allowed, raising=False)

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_DEV", "C_INTERNAL", "What tasks are urgent?")
    )

    assert "urgent task is invoice download" in result


def test_dev_ask_blocks_promised_client_deadline(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="Should not be read",
    )

    async def find_project(db, channel_id):
        return project

    async def should_not_retrieve(*args, **kwargs):
        raise AssertionError("restricted dev ask should not retrieve project context")

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "get_tasks", should_not_retrieve)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", should_not_retrieve)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", should_not_retrieve)
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))

    result = asyncio.run(
        question_answer_service.handle_slash_ask(
            "U_DEV", "C_INTERNAL", "What deadline did we promise the client?"
        )
    )

    assert result == permissions.DEV_ASK_RESTRICTED_MESSAGE


def test_admin_ask_bypasses_dev_restriction(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )

    async def find_project(db, channel_id):
        return project

    async def empty(*args, **kwargs):
        return []

    async def should_not_classify(*args, **kwargs):
        raise AssertionError("admin ask should bypass dev classifier")

    async def answer_chain(question, context, llm_client):
        return AnswerQuestionOutput(answer="Admin-only answer.", sources=[])

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "get_tasks", empty)
    monkeypatch.setattr(question_answer_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(question_answer_service, "get_approved_suggestions", empty)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", empty)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", empty)
    monkeypatch.setattr(question_answer_service, "answer_question_chain", answer_chain)
    monkeypatch.setattr(question_answer_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(permissions, "get_settings", lambda: SimpleNamespace(admin_slack_ids=["U_ADMIN"]))
    monkeypatch.setattr(permissions, "classify_dev_ask_chain", should_not_classify, raising=False)

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_ADMIN", "C_INTERNAL", "What is the project budget?")
    )

    assert result == "Admin-only answer."


def test_dev_ask_filters_admin_only_risk_context(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )
    captured = {}

    async def find_project(db, channel_id):
        return project

    async def empty(*args, **kwargs):
        return []

    async def approved(db, project_id, suggestion_type=None):
        if suggestion_type == "scope_risk":
            return [SimpleNamespace(title="Budget concern", body="SECRET budget risk")]
        return []

    async def memory(project_id, question, limit=5):
        return [
            {"source_type": "scope_risk", "text": "SECRET scope risk", "source_id": None},
            {"source_type": "assumption", "text": "SECRET assumption", "source_id": None},
            {"source_type": "decision", "text": "Use PostgreSQL", "source_id": None},
        ]

    async def answer_chain(question, context, llm_client):
        captured["context"] = context
        return AnswerQuestionOutput(answer="Use PostgreSQL.", sources=[])

    async def allow_dev_ask(*args, **kwargs):
        return True

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "is_dev_ask_allowed", allow_dev_ask)
    monkeypatch.setattr(question_answer_service, "is_global_admin", lambda user_id: False)
    monkeypatch.setattr(question_answer_service, "get_tasks", empty)
    monkeypatch.setattr(question_answer_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(question_answer_service, "get_approved_suggestions", approved)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", memory)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", empty)
    monkeypatch.setattr(question_answer_service, "answer_question_chain", answer_chain)
    monkeypatch.setattr(question_answer_service, "get_llm_client", lambda: object())

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_DEV", "C_INTERNAL", "What should I implement?")
    )

    assert result == "Use PostgreSQL."
    assert "Use PostgreSQL" in captured["context"]
    assert "SECRET" not in captured["context"]
    assert "Noted Scope Risks" not in captured["context"]


def test_admin_ask_keeps_admin_only_risk_context(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )
    captured = {}

    async def find_project(db, channel_id):
        return project

    async def empty(*args, **kwargs):
        return []

    async def approved(db, project_id, suggestion_type=None):
        if suggestion_type == "scope_risk":
            return [SimpleNamespace(title="Budget concern", body="SECRET budget risk")]
        return []

    async def memory(project_id, question, limit=5):
        return [
            {"source_type": "scope_risk", "text": "SECRET scope risk", "source_id": None},
            {"source_type": "assumption", "text": "SECRET assumption", "source_id": None},
        ]

    async def answer_chain(question, context, llm_client):
        captured["context"] = context
        return AnswerQuestionOutput(answer="Admin answer.", sources=[])

    async def allow_dev_ask(*args, **kwargs):
        return True

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "is_dev_ask_allowed", allow_dev_ask)
    monkeypatch.setattr(question_answer_service, "is_global_admin", lambda user_id: True)
    monkeypatch.setattr(question_answer_service, "get_tasks", empty)
    monkeypatch.setattr(question_answer_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(question_answer_service, "get_approved_suggestions", approved)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", memory)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", empty)
    monkeypatch.setattr(question_answer_service, "answer_question_chain", answer_chain)
    monkeypatch.setattr(question_answer_service, "get_llm_client", lambda: object())

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_ADMIN", "C_INTERNAL", "What risks exist?")
    )

    assert result == "Admin answer."
    assert "SECRET budget risk" in captured["context"]
    assert "SECRET scope risk" in captured["context"]
    assert "SECRET assumption" in captured["context"]


def test_qa_task_context_includes_developer_ready_details(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )
    suggestion_id = uuid.uuid4()
    task = SimpleNamespace(
        title="Add invoice download",
        description="Expose a PDF download action from the dashboard.",
        status="todo",
        priority="high",
        source_suggestion_id=suggestion_id,
    )
    suggestion = SimpleNamespace(
        id=suggestion_id,
        payload={
            "acceptance_criteria": ["Clicking an invoice opens a PDF"],
            "dependencies": ["Invoice storage"],
            "blocked_by": ["Confirm whether PDFs already exist"],
        },
    )
    captured = {}

    async def find_project(db, channel_id):
        return project

    async def tasks(db, project_id):
        return [task]

    async def empty(*args, **kwargs):
        return []

    async def approved(db, project_id, suggestion_type=None):
        return [suggestion] if suggestion_type == "task" else []

    async def answer_chain(question, context, llm_client):
        captured["context"] = context
        return AnswerQuestionOutput(answer="Implement invoice download.", sources=[])

    async def allow_dev_ask(*args, **kwargs):
        return True

    monkeypatch.setattr(question_answer_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(question_answer_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(question_answer_service, "is_dev_ask_allowed", allow_dev_ask)
    monkeypatch.setattr(question_answer_service, "is_global_admin", lambda user_id: False)
    monkeypatch.setattr(question_answer_service, "get_tasks", tasks)
    monkeypatch.setattr(question_answer_service, "get_pending_clarifications", empty)
    monkeypatch.setattr(question_answer_service, "get_approved_suggestions", approved)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant", empty)
    monkeypatch.setattr(question_answer_service, "retrieve_relevant_document_chunks", empty)
    monkeypatch.setattr(question_answer_service, "answer_question_chain", answer_chain)
    monkeypatch.setattr(question_answer_service, "get_llm_client", lambda: object())

    result = asyncio.run(
        question_answer_service.handle_slash_ask("U_DEV", "C_INTERNAL", "What should I implement?")
    )

    assert "Implement invoice download" in result
    assert "Expose a PDF download action" in captured["context"]
    assert "Clicking an invoice opens a PDF" in captured["context"]
    assert "Invoice storage" in captured["context"]
    assert "Confirm whether PDFs already exist" in captured["context"]


def test_retrieve_document_chunks_returns_empty_on_embed_failure(monkeypatch):
    class FailingLLM:
        async def embed(self, text):
            raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(document_service, "get_llm_client", lambda: FailingLLM())

    result = asyncio.run(
        document_service.retrieve_relevant_document_chunks(uuid.uuid4(), "question")
    )

    assert result == []


# ── Fix #2: memory from document analysis ─────────────────────────────────────


def test_document_memory_text_includes_summary_and_items():
    document = _completed_document()

    text = document_service._document_memory_text(document)

    assert "spec.pdf" in text
    assert "Customer portal with billing." in text
    assert "Users can download invoices" in text
    assert "Tight billing timeline" in text
    assert "Use Stripe" in text


def test_document_memory_text_empty_without_content():
    document = _completed_document(analysis_summary=None, analysis_items={})

    assert document_service._document_memory_text(document) == ""


def test_store_document_analysis_memory_is_idempotent(monkeypatch):
    document = _completed_document()
    store = []
    embed_calls = []

    async def get_document(db, document_id):
        return document

    async def get_memory_by_source(db, source_type, source_id):
        return list(store)

    async def embed_and_store(project_id, text, source_type, source_id=None, importance=3):
        embed_calls.append((source_type, source_id, text))
        store.append(object())

    monkeypatch.setattr(document_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "get_memory_by_source", get_memory_by_source)
    monkeypatch.setattr(document_service, "embed_and_store", embed_and_store)

    asyncio.run(document_service._store_document_analysis_memory(document.id))
    asyncio.run(document_service._store_document_analysis_memory(document.id))

    assert len(embed_calls) == 1
    assert embed_calls[0][0] == "document_summary"
    assert embed_calls[0][1] == document.id
    assert "Customer portal with billing." in embed_calls[0][2]


def test_store_document_memory_skipped_when_not_completed(monkeypatch):
    document = _completed_document(analysis_status="skipped")
    embed_calls = []

    async def get_document(db, document_id):
        return document

    async def get_memory_by_source(db, source_type, source_id):
        return []

    async def embed_and_store(*args, **kwargs):
        embed_calls.append(args)

    monkeypatch.setattr(document_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(document_service, "get_memory_by_source", get_memory_by_source)
    monkeypatch.setattr(document_service, "embed_and_store", embed_and_store)

    asyncio.run(document_service._store_document_analysis_memory(document.id))

    assert embed_calls == []


def test_embed_and_store_falls_back_to_null_embedding(monkeypatch):
    inserts = []

    class FailingLLM:
        async def embed(self, text):
            raise RuntimeError("embeddings unavailable")

    async def insert_memory_chunk(db, **kwargs):
        inserts.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(memory_service, "get_llm_client", lambda: FailingLLM())
    monkeypatch.setattr(memory_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(memory_service, "insert_memory_chunk", insert_memory_chunk)

    asyncio.run(
        memory_service.embed_and_store(
            uuid.uuid4(), "doc text", "document_summary", source_id=uuid.uuid4()
        )
    )

    assert len(inserts) == 1
    assert inserts[0]["embedding"] is None
    assert inserts[0]["text_content"] == "doc text"


# ── Fix #3: Canvas sync after document processing ─────────────────────────────


def test_document_processing_triggers_canvas_sync(monkeypatch):
    project_id = uuid.uuid4()
    document = SimpleNamespace(id=uuid.uuid4(), project_id=project_id)
    syncs = []

    async def process(document_id, client):
        return None

    async def get_document(db, document_id):
        return document

    async def sync(project_id, reason, client=None):
        syncs.append((project_id, reason))
        return True

    monkeypatch.setattr(document_service, "_process_document", process)
    monkeypatch.setattr(document_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync)

    asyncio.run(document_service.process_document(document.id, client=object()))

    assert syncs == [(project_id, "document_processed")]


def test_canvas_sync_failure_does_not_break_document_processing(monkeypatch):
    document = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    processed = []

    async def process(document_id, client):
        processed.append(document_id)

    async def get_document(db, document_id):
        return document

    async def failing_sync(project_id, reason, client=None):
        raise RuntimeError("canvas down")

    monkeypatch.setattr(document_service, "_process_document", process)
    monkeypatch.setattr(document_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(document_service, "get_document", get_document)
    monkeypatch.setattr(canvas_service, "sync_project_canvas", failing_sync)

    # Must not raise even though the Canvas sync fails.
    asyncio.run(document_service.process_document(document.id, client=object()))

    assert processed == [document.id]


# ── Fix #4: explicit skipped analysis summary ─────────────────────────────────


def test_skipped_analysis_summary_states_counts(monkeypatch):
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_CHUNKS", 200)
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_CHARS", 400_000)
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_PAGES", 100)

    summary = document_service._analysis_skip_reason(
        chunk_count=500, char_count=900_000, page_count=300
    )

    assert summary is not None
    assert "exceeded analysis limits" in summary
    assert "500 chunks" in summary
    assert "900000 characters" in summary
    assert "300 pages" in summary
    assert "first 200 chunks were embedded for bounded retrieval" in summary
    assert "0 chunks were analyzed by the LLM" in summary
