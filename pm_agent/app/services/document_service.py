"""Slack document metadata registration and background processing pipeline."""

import asyncio
import hashlib
import json
import uuid
from typing import Any

import httpx

from pm_agent.app.agents.chains import (
    document_chunk_analysis_chain,
    document_summary_chain,
)
from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.db.document_queries import (
    get_document,
    replace_document_chunks,
    search_similar_document_chunks,
    update_chunk_embedding,
    update_document,
    upsert_document_metadata,
)
from pm_agent.app.db.memory_queries import get_memory_by_source
from pm_agent.app.db.project_queries import find_project_by_channel, get_project_by_id
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.memory_service import embed_and_store
from pm_agent.app.services.slack_service import get_file_info, get_slack_client
from pm_agent.app.utils.document_parsing import (
    DocumentParseError,
    chunk_parsed_document,
    detect_document_type,
    parse_document_bytes,
)
from pm_agent.app.utils.permissions import get_project_channel_kind

logger = get_logger("document_service")

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
DOCUMENT_CONCURRENCY = 3
SUMMARY_BATCH_CHARS = 12000
MAX_ERROR_CHARS = 2000

# Processing caps for automatic analysis. The 25 MiB upload/download cap above
# bounds raw file size, but a large text file can still produce thousands of
# chunks, each triggering an embedding call and an LLM analysis call. These caps
# keep automatic analysis bounded: documents over any cap are still parsed,
# stored, and chunked, but automatic full analysis is skipped.
MAX_DOCUMENT_ANALYSIS_CHUNKS = 200
MAX_DOCUMENT_ANALYSIS_CHARS = 400_000
MAX_DOCUMENT_ANALYSIS_PAGES = 100

# Caps for memory created from a completed document analysis.
MAX_DOCUMENT_MEMORY_ITEMS = 5
MAX_DOCUMENT_MEMORY_CHARS = 4000
DOCUMENT_MEMORY_SOURCE_TYPE = "document_summary"

# Q&A document-chunk retrieval caps (used by the internal-channel /agent ask).
MAX_QA_DOCUMENT_CHUNKS = 6

_active_document_tasks: dict[uuid.UUID, asyncio.Task] = {}


class DocumentDownloadError(RuntimeError):
    """Raised when Slack file bytes cannot be downloaded safely."""


async def register_message_documents(
    project_id: uuid.UUID,
    channel_id: str,
    source_message_id: uuid.UUID,
    files: list[dict],
    client=None,
) -> list[uuid.UUID]:
    """Persist every file in a Slack message, then schedule background work."""
    return await _register_documents(
        project_id=project_id,
        channel_id=channel_id,
        source_message_id=source_message_id,
        files=files,
        client=client,
    )


async def register_file_shared_event(event: dict, client=None) -> list[uuid.UUID]:
    """Persist a standalone file_shared event after resolving its project channel."""
    file_id = event.get("file_id") or (event.get("file") or {}).get("id")
    if not file_id:
        return []

    file_info = dict(event.get("file") or {})
    file_info["id"] = file_id
    file_info.setdefault("user", event.get("user_id"))
    channel_ids = _file_channel_ids(event, file_info)
    project_channel = await _find_file_project_channel(channel_ids)

    if project_channel is None:
        resolved = await get_file_info(client or get_slack_client(), file_id)
        file_info.update({key: value for key, value in resolved.items() if value is not None})
        file_info["id"] = file_id
        file_info.setdefault("user", event.get("user_id"))
        project_channel = await _find_file_project_channel(_file_channel_ids(event, file_info))

    if project_channel is None:
        return []
    project_id, channel_id = project_channel
    return await _register_documents(
        project_id=project_id,
        channel_id=channel_id,
        source_message_id=None,
        files=[file_info],
        client=client,
    )


async def _find_file_project_channel(
    channel_ids: list[str],
) -> tuple[uuid.UUID, str] | None:
    session_factory = get_session_factory()
    async with session_factory() as db:
        for channel_id in channel_ids:
            project = await find_project_by_channel(db, channel_id)
            if project is not None:
                return project.id, channel_id
    return None


def _file_channel_ids(event: dict, file_info: dict) -> list[str]:
    """Collect channel IDs from both event and files.info payload shapes."""
    candidates = [event.get("channel_id"), event.get("channel"), file_info.get("channel_id")]
    candidates.extend(file_info.get("channels") or [])
    candidates.extend(file_info.get("groups") or [])
    for visibility in (file_info.get("shares") or {}).values():
        if isinstance(visibility, dict):
            candidates.extend(visibility.keys())
    return list(dict.fromkeys(channel_id for channel_id in candidates if channel_id))


async def _register_documents(
    project_id: uuid.UUID,
    channel_id: str,
    source_message_id: uuid.UUID | None,
    files: list[dict],
    client=None,
) -> list[uuid.UUID]:
    registrations: list[tuple[uuid.UUID, str, str]] = []
    session_factory = get_session_factory()
    async with session_factory() as db:
        for file_info in files:
            slack_file_id = file_info.get("id")
            if not slack_file_id:
                continue
            document, _inserted = await upsert_document_metadata(
                db,
                project_id=project_id,
                source_message_id=source_message_id,
                slack_file_id=slack_file_id,
                slack_channel_id=channel_id,
                **_metadata_from_slack_file(file_info),
            )
            registrations.append((document.id, document.download_status, document.parse_status))
        await db.commit()

    for document_id, download_status, parse_status in registrations:
        if parse_status == "pending" and download_status in {"pending", "completed"}:
            schedule_document_processing(document_id, client=client)
    return [registration[0] for registration in registrations]


def schedule_document_processing(document_id: uuid.UUID, client=None) -> bool:
    """Schedule one in-process document task, avoiding duplicate local work."""
    active = _active_document_tasks.get(document_id)
    if active is not None and not active.done():
        return False
    task = asyncio.create_task(process_document(document_id, client=client))
    _active_document_tasks[document_id] = task
    task.add_done_callback(lambda completed, did=document_id: _clear_document_task(did, completed))
    return True


def _clear_document_task(document_id: uuid.UUID, completed: asyncio.Task) -> None:
    if _active_document_tasks.get(document_id) is completed:
        _active_document_tasks.pop(document_id, None)


async def process_document(document_id: uuid.UUID, client=None) -> None:
    """Download, parse, chunk, embed, and analyze one persisted document."""
    resolved_client = client or get_slack_client()
    try:
        await _process_document(document_id, resolved_client)
    except Exception as exc:
        logger.error(f"Unexpected document processing failure for {document_id}: {exc}", exc_info=True)
        await _record_unexpected_failure(document_id, str(exc))
    finally:
        # Document processing always ends in a terminal state (parsed/completed,
        # failed, unsupported, or analysis skipped). Reflect it on the Canvas
        # best-effort; a Canvas failure must never affect document processing.
        await _sync_canvas_after_processing(document_id, resolved_client)


async def _sync_canvas_after_processing(document_id: uuid.UUID, client) -> None:
    """Best-effort internal Canvas sync after a document reaches a terminal state."""
    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            document = await get_document(db, document_id)
            project_id = document.project_id if document is not None else None
        if project_id is None:
            return
        from pm_agent.app.services.canvas_service import sync_project_canvas

        await sync_project_canvas(project_id, reason="document_processed", client=client)
    except Exception as exc:
        logger.error(
            f"Best-effort Canvas sync after document {document_id} failed: {exc}",
            exc_info=True,
        )


async def _process_document(document_id: uuid.UUID, client) -> None:
    session_factory = get_session_factory()
    async with session_factory() as db:
        document = await get_document(db, document_id)
        if document is None or document.parse_status in {"parsed", "failed", "unsupported"}:
            return
        if document.download_status in {"failed", "skipped"}:
            return

        if _needs_file_info(document):
            try:
                file_info = await get_file_info(client, document.slack_file_id)
                await update_document(db, document, **_metadata_from_slack_file(file_info))
                await db.commit()
            except Exception as exc:
                await _mark_download_failed(db, document, f"Slack files.info failed: {exc}")
                await db.commit()
                return

        document_type = detect_document_type(document.filename, document.mimetype, document.filetype)
        if document_type is None:
            await update_document(
                db,
                document,
                download_status="skipped",
                download_error=None,
                parse_status="unsupported",
                parse_error=f"Unsupported file type: {document.filetype or document.mimetype or document.filename or 'unknown'}",
                analysis_status="skipped",
            )
            await db.commit()
            return

        if document.size_bytes is not None and document.size_bytes > MAX_DOCUMENT_BYTES:
            await _mark_download_failed(
                db,
                document,
                f"File exceeds the 25 MiB document processing limit ({document.size_bytes} bytes).",
            )
            await db.commit()
            return

        download_url = document.url_private_download or document.url_private
        if not download_url:
            await _mark_download_failed(db, document, "Slack file has no private download URL.")
            await db.commit()
            return

    try:
        data, sha256 = await _download_file(download_url)
    except Exception as exc:
        async with session_factory() as db:
            document = await get_document(db, document_id)
            if document is not None:
                await _mark_download_failed(db, document, str(exc))
                await db.commit()
        return

    async with session_factory() as db:
        document = await get_document(db, document_id)
        if document is None:
            return
        await update_document(
            db,
            document,
            download_status="completed",
            download_error=None,
            sha256=sha256,
            storage_path=None,
        )
        await db.commit()

    try:
        parsed = parse_document_bytes(data, document_type)
        chunks_data = chunk_parsed_document(parsed)
    except DocumentParseError as exc:
        async with session_factory() as db:
            document = await get_document(db, document_id)
            if document is not None:
                await update_document(
                    db,
                    document,
                    parse_status="failed",
                    parse_error=_clean_error(str(exc)),
                    analysis_status="skipped",
                )
                await db.commit()
        return
    finally:
        del data

    analysis_skip_reason = _analysis_skip_reason(
        chunk_count=len(chunks_data),
        char_count=len(parsed.text or ""),
        page_count=parsed.page_count,
    )

    async with session_factory() as db:
        document = await get_document(db, document_id)
        if document is None:
            return
        chunks = await replace_document_chunks(db, document, chunks_data)
        if not chunks:
            analysis_status = "skipped"
            analysis_summary = None
        elif analysis_skip_reason is not None:
            analysis_status = "skipped"
            analysis_summary = analysis_skip_reason
        else:
            analysis_status = "pending"
            analysis_summary = None
        await update_document(
            db,
            document,
            parse_status="parsed",
            parse_error=None,
            extracted_text=parsed.text,
            page_count=parsed.page_count,
            analysis_status=analysis_status,
            analysis_summary=analysis_summary,
        )
        await db.commit()
        chunk_snapshots = [
            {
                "id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "page_number": chunk.page_number,
                "text": chunk.text,
            }
            for chunk in chunks
        ]
        project = await get_project_by_id(db, document.project_id)
        channel_kind = get_project_channel_kind(project, document.slack_channel_id) or "internal"

    if not chunk_snapshots:
        return
    llm_client = get_llm_client()
    # Always bound embedding work so an oversized document cannot trigger
    # thousands of embedding calls, even when analysis is skipped.
    await _embed_chunks(chunk_snapshots[:MAX_DOCUMENT_ANALYSIS_CHUNKS], llm_client)
    if analysis_skip_reason is not None:
        logger.info(f"Skipping automatic analysis for document {document_id}: {analysis_skip_reason}")
        return
    await _analyze_document(document_id, chunk_snapshots, channel_kind, llm_client)


async def _download_file(url: str) -> tuple[bytes, str]:
    from pm_agent.app.config import get_settings

    headers = {"Authorization": f"Bearer {get_settings().slack_bot_token}"}
    buffer = bytearray()
    digest = hashlib.sha256()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as http:
            async with http.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_DOCUMENT_BYTES:
                    raise DocumentDownloadError("File exceeds the 25 MiB document processing limit.")
                async for part in response.aiter_bytes():
                    if len(buffer) + len(part) > MAX_DOCUMENT_BYTES:
                        raise DocumentDownloadError("File exceeds the 25 MiB document processing limit.")
                    buffer.extend(part)
                    digest.update(part)
    except DocumentDownloadError:
        raise
    except Exception as exc:
        raise DocumentDownloadError(f"Slack file download failed: {exc}") from exc
    data = bytes(buffer)
    _validate_downloaded_file_bytes(data)
    return data, digest.hexdigest()


def _validate_downloaded_file_bytes(data: bytes) -> None:
    """Reject Slack auth/login HTML so it is recorded as a download failure."""
    sample = data[:512].lstrip().lower()
    if sample.startswith(b"<!doctype html") or sample.startswith(b"<html"):
        raise DocumentDownloadError(
            "Slack file bytes were not downloaded; received an HTML page instead. "
            "Check Slack file access and ensure the bot has the files:read scope."
        )


async def _embed_chunks(chunks: list[dict], llm_client) -> None:
    semaphore = asyncio.Semaphore(DOCUMENT_CONCURRENCY)

    async def embed(chunk: dict):
        async with semaphore:
            try:
                return chunk["id"], await llm_client.embed(chunk["text"])
            except Exception as exc:
                logger.warning(f"Embedding failed for document chunk {chunk['id']}: {exc}")
                return chunk["id"], None

    results = await asyncio.gather(*(embed(chunk) for chunk in chunks))
    session_factory = get_session_factory()
    async with session_factory() as db:
        for chunk_id, embedding in results:
            if embedding is not None:
                await update_chunk_embedding(db, chunk_id, embedding)
        await db.commit()


def _analysis_skip_reason(chunk_count: int, char_count: int, page_count: int | None) -> str | None:
    """Return a human-readable reason when a document is too large for automatic analysis."""
    reasons: list[str] = []
    if chunk_count > MAX_DOCUMENT_ANALYSIS_CHUNKS:
        reasons.append(
            f"{chunk_count} chunks exceeds the {MAX_DOCUMENT_ANALYSIS_CHUNKS}-chunk analysis cap"
        )
    if char_count > MAX_DOCUMENT_ANALYSIS_CHARS:
        reasons.append(
            f"{char_count} characters exceeds the {MAX_DOCUMENT_ANALYSIS_CHARS}-character analysis cap"
        )
    if page_count is not None and page_count > MAX_DOCUMENT_ANALYSIS_PAGES:
        reasons.append(
            f"{page_count} pages exceeds the {MAX_DOCUMENT_ANALYSIS_PAGES}-page analysis cap"
        )
    if not reasons:
        return None
    embedded_chunks = min(chunk_count, MAX_DOCUMENT_ANALYSIS_CHUNKS)
    page_text = f", {page_count} pages" if page_count is not None else ""
    return (
        "Automatic analysis was skipped because the document exceeded analysis limits ("
        + "; ".join(reasons)
        + f"). The document has {chunk_count} chunks, {char_count} characters{page_text}; "
        "its text and all chunks were stored. "
        f"Only the first {embedded_chunks} chunks were embedded for bounded retrieval, "
        "and 0 chunks were analyzed by the LLM."
    )


async def _analyze_document(document_id: uuid.UUID, chunks: list[dict], channel_kind: str, llm_client) -> None:
    semaphore = asyncio.Semaphore(DOCUMENT_CONCURRENCY)

    async def analyze(chunk: dict):
        async with semaphore:
            return await document_chunk_analysis_chain(
                text=chunk["text"],
                chunk_index=chunk["chunk_index"],
                page_number=chunk["page_number"],
                channel_kind=channel_kind,
                llm_client=llm_client,
            )

    results = await asyncio.gather(*(analyze(chunk) for chunk in chunks), return_exceptions=True)
    failure = next((result for result in results if isinstance(result, Exception)), None)
    session_factory = get_session_factory()
    if failure is not None:
        logger.error(f"Full-document analysis failed for {document_id}: {failure}")
        async with session_factory() as db:
            document = await get_document(db, document_id)
            if document is not None:
                await update_document(db, document, analysis_status="failed")
                await db.commit()
        return

    outputs = list(results)
    analysis_items = merge_document_analysis(outputs)
    try:
        summary = await _summarize_document(outputs, analysis_items, llm_client)
    except Exception as exc:
        logger.error(f"Document summary merge failed for {document_id}: {exc}", exc_info=True)
        async with session_factory() as db:
            document = await get_document(db, document_id)
            if document is not None:
                await update_document(db, document, analysis_status="failed")
                await db.commit()
        return

    async with session_factory() as db:
        document = await get_document(db, document_id)
        if document is not None:
            await update_document(
                db,
                document,
                analysis_status="completed",
                analysis_summary=summary,
                analysis_items=analysis_items,
            )
            await db.commit()

    await _store_document_analysis_memory(document_id)


async def retrieve_relevant_document_chunks(
    project_id: uuid.UUID,
    query: str,
    limit: int = MAX_QA_DOCUMENT_CHUNKS,
) -> list[dict]:
    """Embed a query and return the most relevant document chunks for a project.

    Returns lightweight dicts (never ORM objects) carrying the chunk text plus
    document provenance. On any embedding/search failure this returns [] so Q&A
    degrades gracefully instead of erroring.
    """
    try:
        llm_client = get_llm_client()
        query_embedding = await llm_client.embed(query)
        session_factory = get_session_factory()
        async with session_factory() as db:
            rows = await search_similar_document_chunks(db, project_id, query_embedding, limit)
            return [
                {
                    "text": chunk.text,
                    "document_id": str(document.id),
                    "filename": document.filename,
                    "title": document.title,
                    "page_number": chunk.page_number,
                    "chunk_index": chunk.chunk_index,
                    "permalink": document.permalink,
                    "slack_file_id": document.slack_file_id,
                }
                for chunk, document in rows
            ]
    except Exception as exc:
        logger.error(f"Failed to retrieve document chunks for project {project_id}: {exc}", exc_info=True)
        return []


def _document_memory_text(document) -> str:
    """Build memory text from a completed document analysis summary and items."""
    name = document.title or document.filename or document.slack_file_id
    lines = [f"Document: {name}"]
    content_added = False
    if document.analysis_summary:
        lines.append(f"Summary: {document.analysis_summary}")
        content_added = True

    items = document.analysis_items or {}
    for key, label, field in (
        ("requirements", "Requirements", "text"),
        ("clarifications", "Open questions", "question"),
        ("risks", "Risks", "text"),
        ("decisions", "Decisions", "text"),
    ):
        primaries = []
        for value in list(items.get(key) or [])[:MAX_DOCUMENT_MEMORY_ITEMS]:
            primary = value.get(field) if isinstance(value, dict) else str(value)
            if primary:
                primaries.append(str(primary))
        if primaries:
            lines.append(f"{label}: " + "; ".join(primaries))
            content_added = True

    if not content_added:
        return ""
    return "\n".join(lines).strip()[:MAX_DOCUMENT_MEMORY_CHARS]


async def _store_document_analysis_memory(document_id: uuid.UUID) -> None:
    """Create one project memory chunk from a completed analysis (idempotent, best-effort)."""
    try:
        session_factory = get_session_factory()
        async with session_factory() as db:
            document = await get_document(db, document_id)
            if document is None or document.analysis_status != "completed":
                return
            existing = await get_memory_by_source(db, DOCUMENT_MEMORY_SOURCE_TYPE, document.id)
            if existing:
                return
            project_id = document.project_id
            text = _document_memory_text(document)
        if not text:
            return
        await embed_and_store(
            project_id,
            text,
            DOCUMENT_MEMORY_SOURCE_TYPE,
            source_id=document_id,
            importance=4,
        )
    except Exception as exc:
        logger.error(
            f"Failed to store document analysis memory for {document_id}: {exc}",
            exc_info=True,
        )


def merge_document_analysis(outputs: list[Any]) -> dict:
    """Deduplicate structured chunk outputs with deterministic source selection."""
    specs = {
        "requirements": "text",
        "suggested_tasks": "title",
        "clarifications": "question",
        "decisions": "text",
        "risks": "text",
        "assumptions": "text",
        "features": "name",
    }
    merged = {}
    for collection_name, primary_field in specs.items():
        by_key: dict[str, dict] = {}
        for output in outputs:
            for item in getattr(output, collection_name):
                value = item.model_dump(mode="json")
                key = _normalize_item_key(str(value.get(primary_field) or ""))
                if not key:
                    continue
                existing = by_key.get(key)
                if existing is None or _item_rank(value) > _item_rank(existing):
                    by_key[key] = value
        merged[collection_name] = sorted(
            by_key.values(),
            key=lambda item: (
                item.get("source", {}).get("chunk_index", 0),
                str(item.get(primary_field) or "").lower(),
            ),
        )
    return merged


async def _summarize_document(outputs: list[Any], analysis_items: dict, llm_client) -> str:
    summary_lines = [
        f"Chunk {index}: {output.summary}"
        for index, output in enumerate(outputs)
        if output.summary
    ]
    summary_lines.append("Structured items:\n" + json.dumps(analysis_items, ensure_ascii=False))
    semaphore = asyncio.Semaphore(DOCUMENT_CONCURRENCY)

    async def summarize(context: str) -> str:
        async with semaphore:
            return await document_summary_chain(context, llm_client)

    contexts = _split_summary_context("\n".join(summary_lines), SUMMARY_BATCH_CHARS)
    summaries = await asyncio.gather(*(summarize(context) for context in contexts))
    while len(summaries) > 1:
        contexts = _split_summary_context("\n".join(summaries), SUMMARY_BATCH_CHARS)
        summaries = await asyncio.gather(*(summarize(context) for context in contexts))
    return summaries[0] if summaries else ""


def _split_summary_context(text: str, max_chars: int) -> list[str]:
    if not text:
        return [""]
    return [text[start:start + max_chars] for start in range(0, len(text), max_chars)]


def _metadata_from_slack_file(file_info: dict) -> dict:
    size = file_info.get("size")
    try:
        size_bytes = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_bytes = None
    return {
        "slack_user_id": file_info.get("user"),
        "filename": file_info.get("name"),
        "title": file_info.get("title"),
        "mimetype": file_info.get("mimetype"),
        "filetype": file_info.get("filetype"),
        "pretty_type": file_info.get("pretty_type"),
        "size_bytes": size_bytes,
        "url_private": file_info.get("url_private"),
        "url_private_download": file_info.get("url_private_download"),
        "permalink": file_info.get("permalink"),
    }


def _needs_file_info(document) -> bool:
    return not (
        document.filename
        and (document.mimetype or document.filetype)
        and (document.url_private_download or document.url_private)
    )


async def _mark_download_failed(db, document, error: str) -> None:
    """Mark a download failure / oversized rejection as a terminal failed state.

    Download failure and oversized files can never produce parsed text, so the
    parse stage is also marked failed (not left pending) to avoid a confusing
    "download failed but parse pending" row in Canvas/gather displays.
    """
    cleaned = _clean_error(error)
    await update_document(
        db,
        document,
        download_status="failed",
        download_error=cleaned,
        parse_status="failed",
        parse_error=cleaned,
        analysis_status="skipped",
    )


async def _record_unexpected_failure(document_id: uuid.UUID, error: str) -> None:
    session_factory = get_session_factory()
    async with session_factory() as db:
        document = await get_document(db, document_id)
        if document is None:
            return
        if document.download_status == "pending":
            await _mark_download_failed(db, document, error)
        elif document.parse_status == "pending":
            await update_document(
                db,
                document,
                parse_status="failed",
                parse_error=_clean_error(error),
                analysis_status="skipped",
            )
        else:
            await update_document(db, document, analysis_status="failed")
        await db.commit()


def _normalize_item_key(value: str) -> str:
    return " ".join(
        "".join(character if character.isalnum() else " " for character in value.casefold()).split()
    )


def _item_rank(item: dict) -> tuple[int, int]:
    confidence = {"low": 1, "medium": 2, "high": 3}.get(item.get("confidence"), 0)
    chunk_index = item.get("source", {}).get("chunk_index", 0)
    return confidence, -int(chunk_index)


def _clean_error(error: str) -> str:
    return str(error).strip()[:MAX_ERROR_CHARS]
