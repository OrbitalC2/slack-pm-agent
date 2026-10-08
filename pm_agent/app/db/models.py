"""
SQLAlchemy ORM models for the PM Agent database.

Tables:
    - projects: Maps Slack channels + GitHub repo to one project.
    - processed_events: Prevents duplicate Slack event processing.
    - messages: Raw Slack messages from configured channels.
    - gather_runs: Tracks /agent gather-info executions.
    - suggestions: Pending tasks/clarifications/decisions/risks before approval.
    - tasks: Approved internal tasks.
    - clarifications: Questions needing answers before work is clear.
    - memory_chunks: Summarized project memory with optional embeddings.
"""

import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class Project(Base):
    """Maps Slack channels and GitHub repo to one project."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "internal_canvas_status IN ('not_created','created','failed')",
            name="ck_project_internal_canvas_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_channel_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    internal_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_manager_slack_id: Mapped[str] = mapped_column(Text, nullable=False)
    github_repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_summary: Mapped[str] = mapped_column(
        Text, server_default=sa_text("''"), default=""
    )
    last_gathered_client_ts: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_from_channel_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    internal_canvas_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_canvas_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    internal_canvas_status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="not_created",
        server_default=sa_text("'not_created'"),
    )
    latest_client_update_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_client_update_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    messages: Mapped[list["Message"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    gather_runs: Mapped[list["GatherRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    suggestions: Mapped[list["Suggestion"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    clarifications: Mapped[list["Clarification"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    memory_chunks: Mapped[list["MemoryChunk"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    huddle_runs: Mapped[list["HuddleRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProcessedEvent(Base):
    """Prevents duplicate Slack event processing (Slack may retry events)."""

    __tablename__ = "processed_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing','completed','failed')",
            name="ck_processed_event_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    slack_event_id: Mapped[str] = mapped_column(
        Text, unique=True, nullable=False
    )
    event_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, default="processing", server_default=sa_text("'processing'")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )


class Message(Base):
    """Stores raw Slack messages from client/internal channels."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "slack_channel_id", "slack_message_ts", name="uq_channel_message_ts"
        ),
        CheckConstraint(
            "sender_type IN ('project_manager','client','developer','bot','unknown')",
            name="ck_sender_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    slack_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    slack_message_ts: Mapped[str] = mapped_column(Text, nullable=False)
    slack_thread_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender_slack_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender_type: Mapped[str] = mapped_column(
        Text, default="unknown", server_default=sa_text("'unknown'")
    )
    is_bot: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false")
    )
    message_subtype: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="messages")
    documents: Mapped[list["Document"]] = relationship(back_populates="source_message")


class GatherRun(Base):
    """Tracks each /agent gather-info execution and which message range was processed."""

    __tablename__ = "gather_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','failed')",
            name="ck_gather_run_status",
        ),
        CheckConstraint(
            "notification_status IN ('not_required','pending','sent','failed')",
            name="ck_gather_notification_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    slack_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    started_by_slack_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_slack_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_slack_ts: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, default="running", server_default=sa_text("'running'")
    )
    notification_status: Mapped[str] = mapped_column(
        Text, default="not_required", server_default=sa_text("'not_required'")
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="gather_runs")
    suggestions: Mapped[list["Suggestion"]] = relationship(
        back_populates="gather_run"
    )


class Suggestion(Base):
    """
    Stores pending tasks/clarifications/decisions/risks generated by gather-info
    before project manager approves them.

    suggestion_type: task, clarification, decision, scope_risk, assumption
    status: pending, approved, rejected, edited
    """

    __tablename__ = "suggestions"
    __table_args__ = (
        CheckConstraint(
            "suggestion_type IN ('task','clarification','decision','scope_risk','assumption')",
            name="ck_suggestion_type",
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','edited')",
            name="ck_suggestion_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    gather_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gather_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    suggestion_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_ids: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), default=list
    )
    source_refs: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), default=list
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), default=dict
    )
    status: Mapped[str] = mapped_column(
        Text, default="pending", server_default=sa_text("'pending'")
    )
    approved_by_slack_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="suggestions")
    gather_run: Mapped["GatherRun | None"] = relationship(
        back_populates="suggestions"
    )
    task: Mapped["Task | None"] = relationship(back_populates="source_suggestion")
    clarification: Mapped["Clarification | None"] = relationship(
        back_populates="source_suggestion"
    )


class Task(Base):
    """Stores approved internal tasks."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('todo','doing','blocked','review','done')",
            name="ck_task_status",
        ),
        CheckConstraint(
            "priority IN ('low','medium','high','urgent')",
            name="ck_task_priority",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, default="todo", server_default=sa_text("'todo'")
    )
    owner_slack_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(
        Text, default="medium", server_default=sa_text("'medium'")
    )
    source_message_ids: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), default=list
    )
    source_refs: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), default=list
    )
    source_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suggestions.id", ondelete="SET NULL"),
        nullable=True,
    )
    github_pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_slack_message_ts: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="tasks")
    source_suggestion: Mapped["Suggestion | None"] = relationship(
        back_populates="task"
    )


class Clarification(Base):
    """Stores questions that must be answered before work is clear."""

    __tablename__ = "clarifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','answered','closed')",
            name="ck_clarification_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, default="pending", server_default=sa_text("'pending'")
    )
    assigned_to_slack_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'{}'::jsonb"), default=dict
    )
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_by_slack_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_message_ids: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), default=list
    )
    source_refs: Mapped[dict] = mapped_column(
        JSONB, server_default=sa_text("'[]'::jsonb"), default=list
    )
    source_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suggestions.id", ondelete="SET NULL"),
        nullable=True,
    )
    answer_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="clarifications")
    source_suggestion: Mapped["Suggestion | None"] = relationship(
        back_populates="clarification"
    )
    answer_message: Mapped["Message | None"] = relationship(foreign_keys=[answer_message_id])


class MemoryChunk(Base):
    """
    Stores summarized project memory with optional embeddings for semantic retrieval.

    source_type: gather_summary, huddle_summary, decision, task,
                 clarification, github_summary, manual_note
    """

    __tablename__ = "memory_chunks"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('gather_summary','huddle_summary','decision','scope_risk','assumption','task','clarification','github_summary','manual_note','document_summary')",
            name="ck_memory_source_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1536), nullable=True)
    importance: Mapped[int] = mapped_column(
        Integer, default=3, server_default=sa_text("3")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="memory_chunks")


class HuddleRun(Base):
    """Stores raw manually submitted huddle notes as a provenance source."""

    __tablename__ = "huddle_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa_text("gen_random_uuid()"),
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by_slack_id: Mapped[str] = mapped_column(Text, nullable=False)
    raw_notes: Mapped[str] = mapped_column(Text, nullable=False)
    participants: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="huddle_runs")


class Document(Base):
    """Slack-uploaded document metadata, extracted text, and analysis."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "download_status IN ('pending','completed','failed','skipped')",
            name="ck_document_download_status",
        ),
        CheckConstraint(
            "parse_status IN ('pending','parsed','failed','unsupported')",
            name="ck_document_parse_status",
        ),
        CheckConstraint(
            "analysis_status IN ('pending','completed','failed','skipped')",
            name="ck_document_analysis_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa_text("gen_random_uuid()")
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    slack_file_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    slack_channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    slack_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    mimetype: Mapped[str | None] = mapped_column(Text, nullable=True)
    filetype: Mapped[str | None] = mapped_column(Text, nullable=True)
    pretty_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url_private: Mapped[str | None] = mapped_column(Text, nullable=True)
    url_private_download: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_status: Mapped[str] = mapped_column(
        Text, default="pending", server_default=sa_text("'pending'"), nullable=False
    )
    download_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_status: Mapped[str] = mapped_column(
        Text, default="pending", server_default=sa_text("'pending'"), nullable=False
    )
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_status: Mapped[str] = mapped_column(
        Text, default="pending", server_default=sa_text("'pending'"), nullable=False
    )
    analysis_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis_items: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    analysis_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_included_in_gather_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gather_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    project: Mapped["Project"] = relationship(back_populates="documents")
    source_message: Mapped["Message | None"] = relationship(back_populates="documents")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """Deterministic chunk of parsed document text with optional embedding."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa_text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1536), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()"),
        default=lambda: datetime.now(timezone.utc)
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")
