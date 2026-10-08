"""
Pydantic schemas for LLM input/output validation.

These define the structured JSON formats expected from LLM chains.
"""

from typing import Literal

from pydantic import BaseModel, Field


# ── Gather Info Output ──


class ConfirmedRequirement(BaseModel):
    text: str
    source_message_ids: list[str] = Field(default_factory=list)


class SuggestedTask(BaseModel):
    title: str
    description: str = ""
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    priority: str = "medium"  # low|medium|high|urgent
    confidence: str = "medium"  # low|medium|high
    suggested_owner_slack_id: str | None = None
    source_message_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class OpenClarification(BaseModel):
    question: str  # the unclear point
    why_it_matters: str = ""
    known_context: str = ""
    options: list[str] = Field(default_factory=list)
    recommended_option: str = ""
    answer_owner: str = "client"  # client|admin|internal
    confidence: str = "medium"  # low|medium|high
    source_message_ids: list[str] = Field(default_factory=list)


class ClientDecision(BaseModel):
    decision: str
    source_message_ids: list[str] = Field(default_factory=list)


class ScopeRisk(BaseModel):
    risk: str
    why_it_matters: str = ""
    source_message_ids: list[str] = Field(default_factory=list)


class UnconfirmedAssumption(BaseModel):
    assumption: str
    needs_confirmation_from: str = "client"  # client|project_manager|developer
    source_message_ids: list[str] = Field(default_factory=list)


class GatherInfoOutput(BaseModel):
    """Full output schema for gather-info extraction chain."""

    project_scope_summary: str = ""
    confirmed_requirements: list[ConfirmedRequirement] = Field(default_factory=list)
    suggested_tasks: list[SuggestedTask] = Field(default_factory=list)
    open_clarifications: list[OpenClarification] = Field(default_factory=list)
    client_decisions: list[ClientDecision] = Field(default_factory=list)
    scope_risks: list[ScopeRisk] = Field(default_factory=list)
    unconfirmed_assumptions: list[UnconfirmedAssumption] = Field(default_factory=list)


# ── Decision Request Output ──


class DecisionRequestOutput(BaseModel):
    """Structured decision request built from a developer clarification question."""

    unclear_point: str
    why_it_matters: str = ""
    known_context: str = ""
    options: list[str] = Field(default_factory=list)
    recommended_option: str = ""
    answer_owner: str = "client"  # client|admin|internal
    confidence: str = "medium"  # low|medium|high


# ── Answer Question Output ──


class SourceReference(BaseModel):
    text: str
    message_id: str | None = None
    source_type: str = ""  # message, task, clarification, memory


class AnswerQuestionOutput(BaseModel):
    """Output schema for developer Q&A chain."""

    answer: str
    confidence: str = "medium"  # low|medium|high
    sources: list[SourceReference] = Field(default_factory=list)
    suggested_clarification: str | None = None


class DevAskClassificationOutput(BaseModel):
    """Output schema for dev /agent ask access classification."""

    classification: Literal[
        "allowed_dev_query",
        "restricted_admin_query",
        "unclear",
    ]


# ── Huddle Notes Output ──


class HuddleDecision(BaseModel):
    decision: str
    source_context: str = ""


class HuddleTask(BaseModel):
    title: str
    description: str = ""
    assigned_to: str | None = None
    priority: str = "medium"


class HuddleClarification(BaseModel):
    question: str
    context: str = ""


class HuddleNotesOutput(BaseModel):
    """Output schema for huddle notes extraction chain."""

    summary: str = ""
    decisions: list[HuddleDecision] = Field(default_factory=list)
    tasks: list[HuddleTask] = Field(default_factory=list)
    clarifications: list[HuddleClarification] = Field(default_factory=list)


# ── Project Update Output ──


class ProjectUpdateOutput(BaseModel):
    """Output schema for project update generation chain."""

    summary: str = ""
    completed_items: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


# ── Document Analysis Output ──


class DocumentItemSource(BaseModel):
    page: int | None = None
    chunk_index: int = 0


class DocumentRequirement(BaseModel):
    text: str
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentSuggestedTask(BaseModel):
    title: str
    description: str = ""
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    owner_hint: str | None = None
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentClarification(BaseModel):
    question: str
    reason: str = ""
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentDecision(BaseModel):
    text: str
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentRisk(BaseModel):
    text: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentAssumption(BaseModel):
    text: str
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentFeature(BaseModel):
    name: str
    description: str = ""
    source: DocumentItemSource = Field(default_factory=DocumentItemSource)
    confidence: Literal["low", "medium", "high"] = "medium"


class DocumentChunkAnalysisOutput(BaseModel):
    summary: str = ""
    requirements: list[DocumentRequirement] = Field(default_factory=list)
    suggested_tasks: list[DocumentSuggestedTask] = Field(default_factory=list)
    clarifications: list[DocumentClarification] = Field(default_factory=list)
    decisions: list[DocumentDecision] = Field(default_factory=list)
    risks: list[DocumentRisk] = Field(default_factory=list)
    assumptions: list[DocumentAssumption] = Field(default_factory=list)
    features: list[DocumentFeature] = Field(default_factory=list)
