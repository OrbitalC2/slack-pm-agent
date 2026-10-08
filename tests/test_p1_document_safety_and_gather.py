"""Tests for P1 post-review fixes:

1. Document analysis safety caps (large docs parse but skip analysis).
2. Async parsed documents are not missed by gather-info (carry-over inclusion).
3. Download/oversize failures mark parse_status='failed' (not pending).
4. Historical file registration during gather backfill for existing messages.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from pm_agent.app.agents.schemas import GatherInfoOutput, SuggestedTask
from pm_agent.app.services import document_service, gather_info_service
from pm_agent.app.utils.document_parsing import ParsedDocument


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        return None

    async def rollback(self):
        return None


class GatherSession(FakeSession):
    """FakeSession that also supports the gather_run add/flush lifecycle."""

    def add(self, item):
        self.item = item

    async def flush(self):
        if getattr(self.item, "id", None) is None:
            self.item.id = uuid.uuid4()


def _document(**overrides):
    values = {
        "id": uuid.uuid4(),
        "source_message_id": uuid.uuid4(),
        "slack_file_id": f"F-{uuid.uuid4().hex[:6]}",
        "title": None,
        "filename": "spec.pdf",
        "download_status": "completed",
        "download_error": None,
        "parse_status": "parsed",
        "parse_error": None,
        "analysis_status": "completed",
        "page_count": 2,
        "analysis_summary": "Document summary.",
        "analysis_items": {"requirements": [{"text": "A stored requirement"}]},
        "extracted_text": "full text",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# ── Fix #1: analysis safety caps ──────────────────────────────────────────────


def test_analysis_skip_reason_thresholds(monkeypatch):
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_CHUNKS", 200)
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_CHARS", 400_000)
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_PAGES", 100)

    assert document_service._analysis_skip_reason(10, 1000, 5) is None

    chunk_reason = document_service._analysis_skip_reason(201, 1000, 5)
    assert chunk_reason is not None and "chunk" in chunk_reason

    char_reason = document_service._analysis_skip_reason(10, 400_001, 5)
    assert char_reason is not None and "character" in char_reason

    page_reason = document_service._analysis_skip_reason(10, 1000, 101)
    assert page_reason is not None and "page" in page_reason


def test_large_document_parses_but_skips_analysis(monkeypatch):
    """A large document should parse, store text+chunks, bound embeddings, and skip analysis."""
    monkeypatch.setattr(document_service, "MAX_DOCUMENT_ANALYSIS_CHUNKS", 2)

    document = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        slack_channel_id="C_INTERNAL",
        slack_file_id="F_BIG",
        parse_status="pending",
        download_status="pending",
        analysis_status="pending",
        analysis_summary=None,
        extracted_text=None,
        page_count=None,
        filename="big.txt",
        mimetype="text/plain",
        filetype="txt",
        size_bytes=500,
        url_private_download="https://files.slack.test/F_BIG",
        url_private=None,
    )

    chunks_data = [
        {"chunk_index": index, "page_number": None, "text": f"chunk {index}"}
        for index in range(5)
    ]
    parsed = ParsedDocument(text="BIG DOCUMENT TEXT", pages=[(None, "BIG DOCUMENT TEXT")], page_count=None)

    embedded_batches = []
    analyze_calls = []

    async def fake_download(url):
        return b"data", "sha256"

    def fake_parse(data, document_type):
        return parsed

    def fake_chunk(parsed_doc):
        return chunks_data

    async def fake_replace(db, target, chunks):
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                chunk_index=chunk["chunk_index"],
                page_number=chunk["page_number"],
                text=chunk["text"],
            )
            for chunk in chunks
        ]

    async def fake_get_document(db, document_id):
        return document

    async def fake_update_document(db, target, **values):
        for key, value in values.items():
            setattr(target, key, value)
        return target

    async def fake_get_project(db, project_id):
        return SimpleNamespace(id=project_id)

    async def fake_embed(chunks, llm_client):
        embedded_batches.append(list(chunks))

    async def fake_analyze(document_id, chunks, channel_kind, llm_client):
        analyze_calls.append(document_id)

    monkeypatch.setattr(document_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(document_service, "get_document", fake_get_document)
    monkeypatch.setattr(document_service, "update_document", fake_update_document)
    monkeypatch.setattr(document_service, "_download_file", fake_download)
    monkeypatch.setattr(document_service, "parse_document_bytes", fake_parse)
    monkeypatch.setattr(document_service, "chunk_parsed_document", fake_chunk)
    monkeypatch.setattr(document_service, "replace_document_chunks", fake_replace)
    monkeypatch.setattr(document_service, "get_project_by_id", fake_get_project)
    monkeypatch.setattr(document_service, "get_project_channel_kind", lambda project, channel: "internal")
    monkeypatch.setattr(document_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(document_service, "_embed_chunks", fake_embed)
    monkeypatch.setattr(document_service, "_analyze_document", fake_analyze)

    asyncio.run(document_service._process_document(document.id, object()))

    assert document.parse_status == "parsed"
    assert document.extracted_text == "BIG DOCUMENT TEXT"
    assert document.analysis_status == "skipped"
    assert document.analysis_summary is not None
    assert "skipped" in document.analysis_summary
    assert "exceeded analysis limits" in document.analysis_summary
    assert "first 2 chunks were embedded for bounded retrieval" in document.analysis_summary
    # Analysis must not run, and embeddings must be bounded by the chunk cap.
    assert analyze_calls == []
    assert len(embedded_batches) == 1
    assert len(embedded_batches[0]) == 2


# ── Fix #3: download / oversize failure statuses ──────────────────────────────


def test_mark_download_failed_sets_all_failure_statuses(monkeypatch):
    document = SimpleNamespace(
        download_status="pending",
        download_error=None,
        parse_status="pending",
        parse_error=None,
        analysis_status="pending",
    )

    async def fake_update(db, target, **values):
        for key, value in values.items():
            setattr(target, key, value)
        return target

    monkeypatch.setattr(document_service, "update_document", fake_update)

    asyncio.run(document_service._mark_download_failed(object(), document, "boom"))

    assert document.download_status == "failed"
    assert document.parse_status == "failed"
    assert document.analysis_status == "skipped"
    assert document.download_error == "boom"
    assert document.parse_error == "boom"


def test_download_failure_marks_parse_failed_not_pending(monkeypatch):
    document = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        slack_channel_id="C",
        slack_file_id="F",
        parse_status="pending",
        parse_error=None,
        download_status="pending",
        download_error=None,
        analysis_status="pending",
        filename="spec.pdf",
        mimetype="application/pdf",
        filetype="pdf",
        size_bytes=1000,
        url_private_download="https://files.slack.test/F",
        url_private=None,
    )

    async def fake_get_document(db, document_id):
        return document

    async def fake_update_document(db, target, **values):
        for key, value in values.items():
            setattr(target, key, value)
        return target

    async def boom_download(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(document_service, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(document_service, "get_document", fake_get_document)
    monkeypatch.setattr(document_service, "update_document", fake_update_document)
    monkeypatch.setattr(document_service, "_download_file", boom_download)

    asyncio.run(document_service._process_document(document.id, object()))

    assert document.download_status == "failed"
    assert document.parse_status == "failed"
    assert document.analysis_status == "skipped"
    assert document.parse_error


# ── Fix #2: carry-over of completed documents into gather ─────────────────────


def test_build_document_context_links_carryover_and_consumed():
    completed_linked = _document(analysis_status="completed")
    pending_linked = _document(analysis_status="pending", analysis_summary=None)
    carryover = _document(analysis_status="completed", filename="carryover.pdf")

    linked_rows = [(completed_linked, 4), (pending_linked, 2)]
    carryover_rows = [(carryover, 3)]

    (
        context_by_message,
        refs_by_message,
        carryover_entry,
        consumed_ids,
    ) = gather_info_service._build_document_context(linked_rows, carryover_rows)

    # Linked context is keyed by each document's source message id.
    assert str(completed_linked.source_message_id) in context_by_message
    assert str(pending_linked.source_message_id) in context_by_message

    # Carry-over context is carried by a synthetic transcript entry.
    assert carryover_entry is not None
    assert carryover_entry["id"] == gather_info_service.CARRYOVER_DOCUMENTS_SOURCE_ID
    assert "carryover.pdf" in carryover_entry["normalized_text"]
    assert gather_info_service.CARRYOVER_DOCUMENTS_SOURCE_ID in refs_by_message

    # Only completed analyses are marked consumed; the pending one is excluded.
    assert completed_linked.id in consumed_ids
    assert carryover.id in consumed_ids
    assert pending_linked.id not in consumed_ids


def test_collect_gather_documents_excludes_already_linked(monkeypatch):
    linked = _document()
    other = _document(filename="other.pdf")

    async def fake_linked(db, project_id, message_ids):
        return [(linked, 2)]

    async def fake_unconsumed(db, project_id, channel_id):
        return [(linked, 2), (other, 1)]

    monkeypatch.setattr(gather_info_service, "get_documents_for_messages", fake_linked)
    monkeypatch.setattr(gather_info_service, "get_unconsumed_completed_documents", fake_unconsumed)

    linked_rows, carryover_rows = asyncio.run(
        gather_info_service._collect_gather_documents(
            object(), uuid.uuid4(), "C_INTERNAL", [uuid.uuid4()]
        )
    )

    assert [doc.id for doc, _ in linked_rows] == [linked.id]
    assert [doc.id for doc, _ in carryover_rows] == [other.id]


def test_gather_includes_completed_carryover_document_when_no_new_messages(monkeypatch):
    project = SimpleNamespace(
        id=uuid.uuid4(),
        name="Portal",
        project_manager_slack_id="U_ADMIN",
        client_channel_id="C_CLIENT",
        internal_channel_id="C_INTERNAL",
        project_summary="",
    )
    carryover = _document(filename="late-spec.pdf", analysis_status="completed")

    captured = {"transcripts": [], "suggestions": [], "consumed": None, "completed_ts": "unset"}

    async def find_project(db, channel_id):
        return project

    async def latest(db, project_id, channel_id):
        return SimpleNamespace(to_slack_ts="100.0")

    async def no_messages(*args, **kwargs):
        return []

    async def history(*args, **kwargs):
        return []

    async def linked(db, project_id, message_ids):
        return []

    async def unconsumed(db, project_id, channel_id):
        return [(carryover, 3)]

    async def gather_chain(transcript, llm_client, channel_kind="internal"):
        captured["transcripts"].append(transcript)
        return GatherInfoOutput(
            suggested_tasks=[
                SuggestedTask(
                    title="Implement late-spec requirement",
                    source_message_ids=[gather_info_service.CARRYOVER_DOCUMENTS_SOURCE_ID],
                )
            ]
        )

    async def insert_suggestion(db, project_id, suggestion_type, title, body, gather_run_id, source_message_ids, source_refs, payload):
        captured["suggestions"].append(
            {"type": suggestion_type, "title": title, "source_refs": source_refs}
        )
        return SimpleNamespace(
            id=uuid.uuid4(),
            title=title,
            body=body,
            source_message_ids=source_message_ids,
            source_refs=source_refs,
            payload=payload,
        )

    async def mark_consumed(db, document_ids, gather_run_id):
        captured["consumed"] = (list(document_ids), gather_run_id)

    async def mark_completed(db, gather_run_id, to_slack_ts, notification_status="pending"):
        captured["completed_ts"] = to_slack_ts

    async def update_notification(db, gather_run_id, status):
        return None

    async def sync_canvas(project_id, reason, client=None):
        return True

    async def send_dm(*args, **kwargs):
        return None

    monkeypatch.setattr(gather_info_service, "get_session_factory", lambda: lambda: GatherSession())
    monkeypatch.setattr(gather_info_service, "find_project_by_channel", find_project)
    monkeypatch.setattr(gather_info_service, "can_run_command", lambda user_id, command_name: True)
    monkeypatch.setattr(gather_info_service, "get_project_channel_kind", lambda target, channel: "internal")
    monkeypatch.setattr(gather_info_service, "get_latest_completed_gather_for_channel", latest)
    monkeypatch.setattr(gather_info_service, "get_messages_since", no_messages)
    monkeypatch.setattr(gather_info_service, "fetch_channel_history", history)
    monkeypatch.setattr(gather_info_service, "get_documents_for_messages", linked)
    monkeypatch.setattr(gather_info_service, "get_unconsumed_completed_documents", unconsumed)
    monkeypatch.setattr(gather_info_service, "gather_info_chain", gather_chain)
    monkeypatch.setattr(gather_info_service, "get_llm_client", lambda: object())
    monkeypatch.setattr(gather_info_service, "insert_suggestion", insert_suggestion)
    monkeypatch.setattr(gather_info_service, "mark_documents_consumed", mark_consumed)
    monkeypatch.setattr(gather_info_service, "mark_gather_completed", mark_completed)
    monkeypatch.setattr(gather_info_service, "update_gather_notification_status", update_notification)
    monkeypatch.setattr(gather_info_service, "send_dm", send_dm)

    from pm_agent.app.services import canvas_service

    monkeypatch.setattr(canvas_service, "sync_project_canvas", sync_canvas)

    asyncio.run(gather_info_service.run_gather_info("U_ADMIN", "C_INTERNAL", object()))

    # The carry-over document context reached the gather LLM transcript.
    assert captured["transcripts"], "gather chain was never called"
    assert "late-spec.pdf" in captured["transcripts"][0]

    # A suggestion was generated and carries the document source ref.
    assert captured["suggestions"], "no suggestion was inserted from the carry-over document"
    doc_refs = captured["suggestions"][0]["source_refs"]
    assert {"type": "document", "id": str(carryover.id)} in doc_refs

    # The document was marked consumed for this gather run, and the pointer did
    # not move backward (no new messages -> keep last gathered ts).
    assert captured["consumed"] is not None
    consumed_ids, _run_id = captured["consumed"]
    assert consumed_ids == [carryover.id]
    assert captured["completed_ts"] == "100.0"


# ── Fix #4: historical file registration during backfill ──────────────────────


def test_register_history_file_documents_registers_existing_message(monkeypatch):
    stored_message = SimpleNamespace(id=uuid.uuid4())
    registered = []

    async def get_message(db, channel_id, ts):
        # Message already exists in the DB.
        return stored_message

    async def register(**kwargs):
        registered.append(kwargs)
        return [uuid.uuid4()]

    monkeypatch.setattr(gather_info_service, "get_message_by_channel_ts", get_message)
    monkeypatch.setattr(document_service, "register_message_documents", register)

    project_id = uuid.uuid4()
    slack_msgs = [
        {"ts": "1.0", "text": "no files here"},
        {
            "ts": "2.0",
            "text": "here is the spec",
            "files": [{"id": "F1", "name": "spec.pdf"}],
        },
    ]

    asyncio.run(
        gather_info_service._register_history_file_documents(
            object(), project_id, "C_INTERNAL", slack_msgs, object()
        )
    )

    assert len(registered) == 1
    assert registered[0]["project_id"] == project_id
    assert registered[0]["channel_id"] == "C_INTERNAL"
    assert registered[0]["source_message_id"] == stored_message.id
    assert registered[0]["files"][0]["id"] == "F1"
