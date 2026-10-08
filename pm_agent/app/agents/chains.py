"""
LLM chain implementations — deterministic pipelines around LLM calls.

Each chain has a specific input/output schema. These are NOT autonomous agents;
they are structured extraction pipelines.
"""

from pathlib import Path

from pm_agent.app.agents.llm_client import LLMClient
from pm_agent.app.agents.schemas import (
    AnswerQuestionOutput,
    DecisionRequestOutput,
    DevAskClassificationOutput,
    DocumentChunkAnalysisOutput,
    DocumentItemSource,
    GatherInfoOutput,
    HuddleNotesOutput,
    ProjectUpdateOutput,
)
from pm_agent.app.logging_config import get_logger

logger = get_logger("chains")

# Load prompt templates
PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template from the prompts directory."""
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


async def gather_info_chain(
    transcript: str,
    llm_client: LLMClient,
    channel_kind: str = "client",
) -> GatherInfoOutput:
    """
    Extract structured project information from a project conversation transcript.

    If the transcript is too long, it should be pre-chunked by the caller.
    This chain handles a single transcript segment.
    """
    prompt_name = (
        "gather_internal_info.md"
        if channel_kind == "internal"
        else "gather_client_info.md"
    )
    transcript_label = (
        "Internal Project Conversation Transcript"
        if channel_kind == "internal"
        else "Client Conversation Transcript"
    )
    system_prompt = _load_prompt(prompt_name)
    user_prompt = f"## {transcript_label}\n\n{transcript}"

    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    # Validate through Pydantic
    output = GatherInfoOutput.model_validate(raw)

    # Post-validation: reject tasks without source_message_ids
    valid_tasks = []
    for task in output.suggested_tasks:
        if task.source_message_ids:
            valid_tasks.append(task)
        else:
            logger.warning(f"Rejected task without sources: {task.title}")
    output.suggested_tasks = valid_tasks

    return output


async def merge_gather_outputs(
    partial_outputs: list[GatherInfoOutput],
    llm_client: LLMClient,
) -> GatherInfoOutput:
    """
    Merge multiple partial gather-info outputs into a single coherent output.
    Used when transcript is too long for a single LLM call.
    """
    if len(partial_outputs) == 1:
        return partial_outputs[0]

    # Concatenate all partial data
    merged = GatherInfoOutput()

    for partial in partial_outputs:
        merged.confirmed_requirements.extend(partial.confirmed_requirements)
        merged.suggested_tasks.extend(partial.suggested_tasks)
        merged.open_clarifications.extend(partial.open_clarifications)
        merged.client_decisions.extend(partial.client_decisions)
        merged.scope_risks.extend(partial.scope_risks)
        merged.unconfirmed_assumptions.extend(partial.unconfirmed_assumptions)

    # Use LLM to create a unified scope summary from all partials
    summaries = "\n\n".join(
        f"Chunk {i+1} summary: {p.project_scope_summary}"
        for i, p in enumerate(partial_outputs)
        if p.project_scope_summary
    )

    if summaries:
        system_prompt = (
            "You are a project scoping assistant. Merge the following partial "
            "project scope summaries into one coherent, deduplicated summary. "
            "Return JSON: {\"project_scope_summary\": \"merged summary\"}"
        )
        raw = await llm_client.generate_json(
            system_prompt=system_prompt,
            user_prompt=summaries,
        )
        merged.project_scope_summary = raw.get("project_scope_summary", "")

    # Deduplicate tasks by title similarity (simple approach)
    seen_titles = set()
    unique_tasks = []
    for task in merged.suggested_tasks:
        normalized = task.title.lower().strip()
        if normalized not in seen_titles:
            seen_titles.add(normalized)
            unique_tasks.append(task)
    merged.suggested_tasks = unique_tasks

    return merged


async def answer_question_chain(
    question: str,
    context: str,
    llm_client: LLMClient,
) -> AnswerQuestionOutput:
    """
    Answer a developer question using retrieved project context.
    """
    system_prompt = _load_prompt("answer_question.md")
    user_prompt = (
        f"## Question\n\n{question}\n\n"
        f"## Available Project Context\n\n{context}"
    )

    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    return AnswerQuestionOutput.model_validate(raw)


async def build_decision_request_chain(
    question: str,
    context: str,
    llm_client: LLMClient,
) -> DecisionRequestOutput:
    """Turn a developer question + project context into a structured decision request."""
    system_prompt = _load_prompt("build_decision_request.md")
    user_prompt = (
        f"## Developer Question\n\n{question}\n\n"
        f"## Available Project Context\n\n{context}"
    )

    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    output = DecisionRequestOutput.model_validate(raw)
    if not output.unclear_point:
        output.unclear_point = question.strip()
    return output


async def classify_dev_ask_chain(
    question: str,
    llm_client: LLMClient,
) -> DevAskClassificationOutput:
    """Classify whether a dev may ask a project question."""
    system_prompt = _load_prompt("dev_ask_classification.md")
    user_prompt = f"## Question\n\n{question}"

    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    return DevAskClassificationOutput.model_validate(raw)


async def huddle_notes_chain(
    notes: str,
    llm_client: LLMClient,
    participants: str | None = None,
) -> HuddleNotesOutput:
    """
    Extract decisions, tasks, and clarifications from manual huddle notes.
    Supports Urdu, English, and Roman Urdu.
    """
    system_prompt = _load_prompt("huddle_notes.md")
    user_prompt = f"## Huddle Notes\n\n{notes}"
    if participants:
        user_prompt += f"\n\n## Participants\n\n{participants}"

    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    return HuddleNotesOutput.model_validate(raw)


async def project_update_chain(
    project_data: str,
    llm_client: LLMClient,
) -> ProjectUpdateOutput:
    """
    Generate a client-ready project update draft.
    """
    system_prompt = _load_prompt("project_update.md")
    user_prompt = f"## Project Data\n\n{project_data}"

    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    return ProjectUpdateOutput.model_validate(raw)


async def document_chunk_analysis_chain(
    text: str,
    chunk_index: int,
    page_number: int | None,
    channel_kind: str,
    llm_client: LLMClient,
) -> DocumentChunkAnalysisOutput:
    """Extract structured project facts from one document chunk."""
    system_prompt = _load_prompt("document_chunk_analysis.md")
    user_prompt = (
        f"## Channel kind\n{channel_kind}\n\n"
        f"## Chunk index\n{chunk_index}\n\n"
        f"## Page\n{page_number if page_number is not None else 'null'}\n\n"
        f"## Document chunk\n{text}"
    )
    raw = await llm_client.generate_json(system_prompt=system_prompt, user_prompt=user_prompt)
    output = DocumentChunkAnalysisOutput.model_validate(raw)
    source = DocumentItemSource(page=page_number, chunk_index=chunk_index)
    for collection_name in (
        "requirements",
        "suggested_tasks",
        "clarifications",
        "decisions",
        "risks",
        "assumptions",
        "features",
    ):
        for item in getattr(output, collection_name):
            item.source = source.model_copy()
    return output


async def document_summary_chain(context: str, llm_client: LLMClient) -> str:
    """Merge compact document summaries/items into one summary."""
    system_prompt = _load_prompt("document_summary.md")
    raw = await llm_client.generate_json(
        system_prompt=system_prompt,
        user_prompt=f"## Document analysis context\n\n{context}",
    )
    return str(raw.get("summary") or "").strip()[:3000]
