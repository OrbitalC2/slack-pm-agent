"""
Developer clarification service — backs `/agent clarify <question>`.

A developer can raise a clarification directly. The agent searches existing
confirmed answers first (answered clarifications + project memory/docs). If a
reliable answer already exists it is returned with its source and no new
clarification is created. Otherwise a structured decision request is created and
DM'd to admins for review. This command never creates tasks.
"""

import re
import uuid

from pm_agent.app.agents.chains import (
    answer_question_chain,
    build_decision_request_chain,
)
from pm_agent.app.agents.llm_client import get_llm_client
from pm_agent.app.db.memory_queries import get_clarifications
from pm_agent.app.db.project_queries import find_project_by_channel
from pm_agent.app.db.session import get_session_factory
from pm_agent.app.db.suggestion_queries import (
    get_pending_suggestions,
    insert_suggestion,
)
from pm_agent.app.db.task_queries import get_tasks
from pm_agent.app.logging_config import get_logger
from pm_agent.app.services.memory_service import retrieve_relevant
from pm_agent.app.services.slack_service import send_dm
from pm_agent.app.utils.permissions import (
    DEV_ASK_RESTRICTED_MESSAGE,
    get_global_admin_slack_ids,
    is_dev_ask_allowed,
    is_internal_project_channel,
)
from pm_agent.app.utils.slack_formatting import decision_request_dm_blocks

logger = get_logger("clarification")

CLARIFY_INTERNAL_ONLY = (
    "❌ `/agent clarify` is only available in the internal project channel."
)
NO_PROJECT = "❌ No project configured for this channel."
CONFIDENT_ANSWER_LABEL = "high"
MAX_CONTEXT_CHARS = 4000
NO_ADMINS_CONFIGURED = (
    "❌ No global admins are configured to review clarification requests. "
    "Set GLOBAL_ADMIN_SLACK_IDS before creating new requests."
)


async def handle_clarify_command(
    user_id: str,
    channel_id: str,
    question: str,
    client,
) -> str:
    """Handle `/agent clarify <question>`; returns a private reply for the developer."""
    question = (question or "").strip()
    if not question:
        return (
            "Please include your question. "
            "Example: `/agent clarify should cancelled users be able to download old invoices?`"
        )

    session_factory = get_session_factory()
    async with session_factory() as db:
        project = await find_project_by_channel(db, channel_id)
        if not project:
            return NO_PROJECT
        # Client channel (and any non-internal channel) stays blocked/private.
        if not is_internal_project_channel(project, channel_id):
            return CLARIFY_INTERNAL_ONLY

        # Permission / safety: mirror dev ask gating.
        if not await is_dev_ask_allowed(user_id, question, project):
            return DEV_ASK_RESTRICTED_MESSAGE

        project_id = project.id
        project_name = project.name

        # 1) Search existing confirmed answers first.
        answered = await get_clarifications(db, project_id, status="answered")
        pending = await get_pending_suggestions(db, project_id, "clarification")
        pending_clar = await get_clarifications(db, project_id, status="pending")
        context_text = await _build_context(db, project)

    matched_answer = _match_answered_clarification(question, answered)
    if matched_answer is not None:
        return _format_answer_reply(matched_answer)

    # 2) Reliable confirmed answer from memory/docs via the Q&A chain.
    confident = await _confident_answer(project_id, question, context_text)
    if confident is not None:
        return confident

    # 3) Do not duplicate an already-open clarification.
    if _matches_existing(question, pending, pending_clar):
        return (
            "ℹ️ A similar clarification is already pending admin review, so I did not "
            "create a duplicate."
        )

    admin_ids = get_global_admin_slack_ids()
    if not admin_ids:
        return NO_ADMINS_CONFIGURED

    # 4) No confirmed answer — build a structured decision request and DM admins.
    try:
        llm_client = get_llm_client()
        decision = await build_decision_request_chain(question, context_text, llm_client)
    except Exception as e:
        logger.error(f"Failed to build decision request: {e}", exc_info=True)
        return "❌ I could not process your clarification right now. Please try again."

    payload = {
        "why_it_matters": decision.why_it_matters,
        "known_context": decision.known_context,
        "options": list(decision.options),
        "recommended_option": decision.recommended_option,
        "answer_owner": decision.answer_owner or "client",
        "confidence": decision.confidence,
        "submitter_slack_id": user_id,
        "source_type": "dev_clarify",
        "channel_kind": "internal",
    }

    async with session_factory() as db:
        suggestion = await insert_suggestion(
            db,
            project_id=project_id,
            suggestion_type="clarification",
            title=decision.unclear_point or question,
            body=decision.why_it_matters or "",
            payload=payload,
        )
        suggestion_dict = {
            "id": suggestion.id,
            "suggestion_type": "clarification",
            "title": suggestion.title,
            "body": suggestion.body,
            "source_message_ids": [],
            "source_refs": [],
            "payload": suggestion.payload,
            "gather_run_id": "",
        }
        await db.commit()

    await _dm_admins_decision_request(client, project_name, suggestion_dict, admin_ids)

    return (
        "I could not find a confirmed answer. I created a clarification request for "
        "admin review."
    )


async def _build_context(db, project) -> str:
    """Assemble compact project context for decision-request framing."""
    parts = []
    if project.project_summary:
        parts.append(f"## Project Summary\n{project.project_summary}")

    tasks = await get_tasks(db, project.id)
    if tasks:
        task_lines = [f"- [{t.status}] {t.title}" for t in tasks[:15]]
        parts.append("## Current Tasks\n" + "\n".join(task_lines))

    answered = await get_clarifications(db, project.id, status="answered")
    if answered:
        ans_lines = [
            f"- Q: {c.question} | A: {c.final_answer}"
            for c in answered[:10]
            if c.final_answer
        ]
        if ans_lines:
            parts.append("## Answered Clarifications\n" + "\n".join(ans_lines))

    text = "\n\n".join(parts) if parts else "No project context available."
    return text[:MAX_CONTEXT_CHARS]


async def _confident_answer(
    project_id: uuid.UUID,
    question: str,
    context_text: str,
) -> str | None:
    """Return a formatted answer only when the Q&A chain is highly confident."""
    try:
        memory_results = await retrieve_relevant(project_id, question, limit=5)
        memory_context = ""
        if memory_results:
            memory_context = "\n\n## Relevant Memory\n" + "\n".join(
                f"- [{m.get('source_type')}] {m.get('text')}" for m in memory_results
            )
        full_context = (context_text + memory_context)[: MAX_CONTEXT_CHARS + 2000]

        llm_client = get_llm_client()
        output = await answer_question_chain(question, full_context, llm_client)
    except Exception as e:
        logger.warning(f"Clarify confident-answer lookup failed: {e}", exc_info=True)
        return None

    if (output.confidence or "").lower() != CONFIDENT_ANSWER_LABEL:
        return None
    if not (output.answer or "").strip():
        return None

    reply = f"✅ Found a confirmed answer:\n{output.answer}"
    if output.sources:
        source_texts = [f"• {s.text}" for s in output.sources[:5] if s.text]
        if source_texts:
            reply += "\n\n📎 *Sources:*\n" + "\n".join(source_texts)
    return reply


def _match_answered_clarification(question: str, answered: list):
    """Return an answered clarification whose question matches, else None."""
    normalized = _normalize(question)
    for clar in answered:
        if not clar.final_answer:
            continue
        if _is_similar(normalized, _normalize(clar.question)):
            return clar
    return None


def _matches_existing(question: str, pending_suggestions: list, pending_clarifications: list) -> bool:
    """Return True if an unresolved clarification already covers this question."""
    normalized = _normalize(question)
    for item in pending_suggestions:
        if _is_similar(normalized, _normalize(item.title)):
            return True
    for clar in pending_clarifications:
        if _is_similar(normalized, _normalize(clar.question)):
            return True
    return False


def _format_answer_reply(clarification) -> str:
    reply = f"✅ Found a confirmed answer:\n{clarification.final_answer}"
    if clarification.answered_by_slack_id:
        reply += f"\n\n_Answered by <@{clarification.answered_by_slack_id}>._"
    return reply


async def _dm_admins_decision_request(
    client,
    project_name: str,
    suggestion_dict: dict,
    admin_ids: list[str] | None = None,
) -> None:
    blocks = decision_request_dm_blocks(project_name, suggestion_dict)
    admin_ids = admin_ids if admin_ids is not None else get_global_admin_slack_ids()
    for admin_id in admin_ids:
        try:
            await send_dm(
                client,
                admin_id,
                text=f"Decision request for {project_name}",
                blocks=blocks,
            )
        except Exception as e:
            logger.error(f"Failed to DM admin {admin_id} decision request: {e}", exc_info=True)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (text or "").strip().lower())).strip()


def _is_similar(a: str, b: str) -> bool:
    """Lightweight question similarity: containment or high token overlap."""
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    tokens_a = {t for t in a.split() if len(t) > 3}
    tokens_b = {t for t in b.split() if len(t) > 3}
    if not tokens_a or not tokens_b:
        return False
    overlap = len(tokens_a & tokens_b)
    smaller = min(len(tokens_a), len(tokens_b))
    return smaller > 0 and overlap / smaller >= 0.7
