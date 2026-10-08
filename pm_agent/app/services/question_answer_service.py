"""
Question answer service — developer Q&A with project context.

Follows spec §5.4 retrieval order:
1. Identify project from channel
2. Retrieve structured DB data (summary, tasks, clarifications, approved decisions)
3. Vector search memory_chunks
4. Load referenced raw messages if needed
5. Build context, call LLM, reply with sources
"""

import re
import uuid

from pm_agent.app.agents.chains import answer_question_chain
from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.db.message_queries import get_messages_by_ids
from pm_agent.app.db.memory_queries import get_pending_clarifications
from pm_agent.app.db.models import Clarification, Suggestion, Task
from pm_agent.app.db.project_queries import find_project_by_channel
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.db.suggestion_queries import get_approved_suggestions
from pm_agent.app.db.task_queries import get_tasks
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.document_service import retrieve_relevant_document_chunks
from pm_agent.app.services.memory_service import retrieve_relevant
from pm_agent.app.utils.permissions import (
    DEV_ASK_RESTRICTED_MESSAGE,
    is_global_admin,
    is_dev_ask_allowed,
    is_internal_project_channel,
)
from pm_agent.app.utils.source_refs import slack_message_source_ref

logger = get_logger("question_answer")
MAX_MEMORY_TEXT_CHARS = 300
MAX_QA_SOURCE_CHARS = 600
MAX_QA_SOURCE_REFS = 5
MAX_QA_TASK_DETAIL_CHARS = 300

# Document-excerpt retrieval caps for internal-channel Q&A.
MAX_QA_DOCUMENT_CHUNKS = 6
MAX_QA_DOCUMENT_CHUNK_CHARS = 700
MAX_QA_DOCUMENT_CONTEXT_CHARS = 3500

MEMORY_SOURCE_MODELS = {
    "decision": Suggestion,
    "scope_risk": Suggestion,
    "assumption": Suggestion,
    "task": Task,
    "clarification": Clarification,
}
DEV_RESTRICTED_MEMORY_SOURCE_TYPES = {"scope_risk", "assumption"}


async def handle_developer_question(event: dict, say, context: dict) -> None:
    """Handle @agent mention in internal channel."""
    channel_id = event.get("channel")
    user_id = event.get("user")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts") or event.get("ts")

    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project or not is_internal_project_channel(project, channel_id):
            return

    # Strip bot mention from question
    question = _strip_mention(text)
    if not question:
        await say(text="What would you like to know? Mention me with a question!", thread_ts=thread_ts)
        return

    answer = await _answer_question(user_id, channel_id, question)
    await say(text=answer, thread_ts=thread_ts)


async def handle_slash_ask(user_id: str, channel_id: str, question: str) -> str:
    """Handle /agent ask <question>."""
    return await _answer_question(user_id, channel_id, question)


async def should_silence_app_mention_error(channel_id: str | None) -> bool:
    """Return true when an app-mention failure must not produce a public reply."""
    if not channel_id:
        return True

    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        return bool(project and not is_internal_project_channel(project, channel_id))


async def _answer_question(user_id: str, channel_id: str, question: str) -> str:
    """Core Q&A logic — retrieves context and generates answer."""
    session_factory = get_session_factory()

    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            return "❌ No project configured for this channel."
        if not is_internal_project_channel(project, channel_id):
            return "❌ `/agent ask` is only available in the internal project channel."
        if not await is_dev_ask_allowed(user_id, question, project):
            return DEV_ASK_RESTRICTED_MESSAGE
        user_is_admin = is_global_admin(user_id)

        # Step 2: Retrieve structured DB data
        context_parts = []

        # Project summary
        if project.project_summary:
            context_parts.append(f"## Project Summary\n{project.project_summary}")

        # Tasks
        tasks = await get_tasks(db, project.id)
        if tasks:
            approved_task_suggestions = await get_approved_suggestions(db, project.id, "task")
            task_suggestion_by_id = {
                str(suggestion.id): suggestion for suggestion in approved_task_suggestions
            }
            task_lines = [
                _task_context_line(t, task_suggestion_by_id.get(str(t.source_suggestion_id)))
                for t in tasks[:20]
            ]
            context_parts.append(f"## Current Tasks\n" + "\n".join(task_lines))

        # Clarifications
        clarifications = await get_pending_clarifications(db, project.id)
        if clarifications:
            clar_lines = [f"- {c.question}" for c in clarifications[:10]]
            context_parts.append(f"## Pending Clarifications\n" + "\n".join(clar_lines))

        # Approved decisions (from suggestions, per decision #11)
        approved_decisions = await get_approved_suggestions(db, project.id, "decision")
        if approved_decisions:
            dec_lines = [f"- {d.title}" for d in approved_decisions[:10]]
            context_parts.append(f"## Approved Decisions\n" + "\n".join(dec_lines))

        # Approved scope-related suggestions
        if user_is_admin:
            approved_risks = await get_approved_suggestions(db, project.id, "scope_risk")
            if approved_risks:
                risk_lines = [f"- {r.title}: {r.body}" for r in approved_risks[:10]]
                context_parts.append(f"## Noted Scope Risks\n" + "\n".join(risk_lines))

    # Step 3: Vector search memory_chunks
    memory_results = await retrieve_relevant(project.id, question, limit=5)
    if not user_is_admin:
        memory_results = [
            memory for memory in memory_results
            if memory.get("source_type") not in DEV_RESTRICTED_MEMORY_SOURCE_TYPES
        ]
    if memory_results:
        async with session_factory() as db:
            mem_lines = await _memory_context_lines(db, memory_results)
        context_parts.append(f"## Relevant Memory\n" + "\n".join(mem_lines))

    # Step 4: Vector search document_chunks (internal channel only — reached only
    # after the internal-channel guard above, so document content never leaks to
    # the client channel).
    document_results = await retrieve_relevant_document_chunks(
        project.id, question, limit=MAX_QA_DOCUMENT_CHUNKS
    )
    document_lines = _document_context_lines(document_results)
    if document_lines:
        context_parts.append("## Relevant Document Excerpts\n" + "\n".join(document_lines))

    # Step 6: Build context and call LLM
    full_context = "\n\n".join(context_parts) if context_parts else "No project context available."

    try:
        llm_client = get_llm_client()
        output = await answer_question_chain(question, full_context, llm_client)

        # Format response
        response = output.answer

        if output.sources:
            source_texts = [f"• {s.text}" for s in output.sources[:5]]
            response += "\n\n📎 *Sources:*\n" + "\n".join(source_texts)

        if output.suggested_clarification:
            response += f"\n\n❓ *Suggested clarification:* {output.suggested_clarification}"

        return response

    except Exception as e:
        logger.error(f"Q&A chain failed: {e}", exc_info=True)
        return "❌ I encountered an error answering your question. Please try again."


def _strip_mention(text: str) -> str:
    """Strip @bot mention from text to extract the actual question."""
    # Remove <@UXXXXX> patterns
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    return cleaned


def _document_context_lines(document_results: list[dict]) -> list[str]:
    """Format retrieved document chunks into capped, source-linked context lines."""
    lines = []
    total_chars = 0
    for result in document_results[:MAX_QA_DOCUMENT_CHUNKS]:
        text = _truncate(result.get("text") or "", MAX_QA_DOCUMENT_CHUNK_CHARS)
        if not text:
            continue
        if total_chars + len(text) > MAX_QA_DOCUMENT_CONTEXT_CHARS:
            break
        total_chars += len(text)

        name = result.get("title") or result.get("filename") or result.get("document_id")
        location = []
        if result.get("page_number") is not None:
            location.append(f"p.{result['page_number']}")
        location.append(f"chunk {result.get('chunk_index')}")
        header = f"- [{name} ({', '.join(location)})]"
        permalink = result.get("permalink")
        if permalink:
            header += f" {permalink}"
        lines.append(f"{header}\n  {text}")
    return lines


def _task_context_line(task, source_suggestion) -> str:
    """Format task context with bounded implementation details for Q&A."""
    line = f"- [{task.status}] {task.title} (priority: {task.priority})"
    if task.description:
        line += f"\n  Description: {_truncate(task.description, MAX_QA_TASK_DETAIL_CHARS)}"

    payload = getattr(source_suggestion, "payload", None) or {}
    criteria = [str(c).strip() for c in (payload.get("acceptance_criteria") or []) if str(c).strip()]
    if criteria:
        line += "\n  Acceptance: " + _truncate("; ".join(criteria[:3]), MAX_QA_TASK_DETAIL_CHARS)

    dependencies = [str(d).strip() for d in (payload.get("dependencies") or []) if str(d).strip()]
    if dependencies:
        line += "\n  Depends on: " + _truncate("; ".join(dependencies[:3]), MAX_QA_TASK_DETAIL_CHARS)

    blockers = [str(b).strip() for b in (payload.get("blocked_by") or []) if str(b).strip()]
    if blockers:
        line += "\n  Blocked by: " + _truncate("; ".join(blockers[:3]), MAX_QA_TASK_DETAIL_CHARS)
    return line


async def _memory_context_lines(db, memory_results: list[dict]) -> list[str]:
    """Return capped memory context lines with expanded source refs when possible."""
    lines = []
    for memory in memory_results[:5]:
        source_type = memory.get("source_type") or "memory"
        text = _truncate(memory.get("text") or "", MAX_MEMORY_TEXT_CHARS)
        line = f"- [{source_type}] {text}"
        source_text = await _memory_source_context(db, memory)
        if source_text:
            line += f"\n  Sources: {source_text}"
        lines.append(line)
    return lines


async def _memory_source_context(db, memory: dict) -> str:
    """Resolve a memory hit's source record to stored Slack/huddle source refs."""
    source_id = _parse_uuid(memory.get("source_id"))
    if not source_id:
        return ""

    source_type = memory.get("source_type")
    model = MEMORY_SOURCE_MODELS.get(source_type)
    if not model:
        return ""

    source_obj = await db.get(model, source_id)
    if source_obj is None:
        return ""

    refs = list(getattr(source_obj, "source_refs", None) or [])
    if not refs:
        refs = await _refs_from_source_message_ids(db, source_obj)
    return _format_source_refs(refs)


async def _refs_from_source_message_ids(db, source_obj) -> list[dict]:
    """Build source refs from source_message_ids when source_refs are absent."""
    message_ids = []
    for raw_id in getattr(source_obj, "source_message_ids", None) or []:
        parsed = _parse_uuid(raw_id)
        if parsed:
            message_ids.append(parsed)
    if not message_ids:
        return []
    messages = await get_messages_by_ids(db, message_ids)
    return [slack_message_source_ref(message) for message in messages]


def _format_source_refs(refs: list[dict]) -> str:
    """Format source refs for the Q&A prompt with conservative caps."""
    parts = []
    for ref in refs[:MAX_QA_SOURCE_REFS]:
        if ref.get("type") == "slack_message":
            label = ref.get("permalink") or (
                f"Slack message {ref.get('channel_id')}:{ref.get('slack_message_ts')}"
            )
            parts.append(label)
        elif ref.get("type") == "huddle_run":
            parts.append(f"huddle notes {ref.get('huddle_run_id')}")
    return _truncate("; ".join(part for part in parts if part), MAX_QA_SOURCE_CHARS)


def _parse_uuid(value) -> uuid.UUID | None:
    """Parse UUID-ish values without raising into Q&A flow."""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _truncate(text: str, max_chars: int) -> str:
    """Cap prompt context strings safely."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
