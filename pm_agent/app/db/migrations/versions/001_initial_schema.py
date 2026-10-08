"""initial schema - all mvp tables

Revision ID: 001_initial
Revises:
Create Date: 2026-06-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Projects
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column("client_channel_id", sa.Text(), nullable=False, unique=True),
        sa.Column("internal_channel_id", sa.Text(), nullable=True),
        sa.Column("project_manager_slack_id", sa.Text(), nullable=False),
        sa.Column("github_repo_url", sa.Text(), nullable=True),
        sa.Column("project_summary", sa.Text(), server_default=sa.text("''")),
        sa.Column("last_gathered_client_ts", sa.Text(), nullable=True),
        sa.Column("created_from_channel_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Processed events (deduplication)
    op.create_table(
        "processed_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slack_event_id", sa.Text(), unique=True, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # Messages
    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("slack_channel_id", sa.Text(), nullable=False),
        sa.Column("slack_message_ts", sa.Text(), nullable=False),
        sa.Column("slack_thread_ts", sa.Text(), nullable=True),
        sa.Column("sender_slack_id", sa.Text(), nullable=True),
        sa.Column("sender_type", sa.Text(), server_default=sa.text("'unknown'")),
        sa.Column("is_bot", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("message_subtype", sa.Text(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("slack_channel_id", "slack_message_ts", name="uq_channel_message_ts"),
        sa.CheckConstraint("sender_type IN ('project_manager','client','developer','bot','unknown')", name="ck_sender_type"),
    )

    # Gather runs
    op.create_table(
        "gather_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("started_by_slack_id", sa.Text(), nullable=False),
        sa.Column("from_slack_ts", sa.Text(), nullable=True),
        sa.Column("to_slack_ts", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('running','completed','failed')", name="ck_gather_run_status"),
    )

    # Suggestions
    op.create_table(
        "suggestions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("gather_run_id", UUID(as_uuid=True), sa.ForeignKey("gather_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("suggestion_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_message_ids", JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("payload", JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'")),
        sa.Column("approved_by_slack_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("suggestion_type IN ('task','clarification','decision','scope_risk','assumption')", name="ck_suggestion_type"),
        sa.CheckConstraint("status IN ('pending','approved','rejected','edited')", name="ck_suggestion_status"),
    )

    # Tasks
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'todo'")),
        sa.Column("owner_slack_id", sa.Text(), nullable=True),
        sa.Column("priority", sa.Text(), server_default=sa.text("'medium'")),
        sa.Column("source_message_ids", JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_suggestion_id", UUID(as_uuid=True), sa.ForeignKey("suggestions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("github_pr_url", sa.Text(), nullable=True),
        sa.Column("internal_slack_message_ts", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('todo','doing','blocked','review','done')", name="ck_task_status"),
        sa.CheckConstraint("priority IN ('low','medium','high','urgent')", name="ck_task_priority"),
    )

    # Clarifications
    op.create_table(
        "clarifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'")),
        sa.Column("assigned_to_slack_id", sa.Text(), nullable=True),
        sa.Column("source_message_ids", JSONB, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_suggestion_id", UUID(as_uuid=True), sa.ForeignKey("suggestions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("answer_message_id", UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('pending','answered','closed')", name="ck_clarification_status"),
    )

    # Memory chunks (with pgvector)
    op.create_table(
        "memory_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("importance", sa.Integer(), server_default=sa.text("3")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "source_type IN ('gather_summary','huddle_summary','decision','task','clarification','github_summary','manual_note')",
            name="ck_memory_source_type",
        ),
    )

    # Add vector column separately (Alembic doesn't natively support pgvector type)
    op.execute("ALTER TABLE memory_chunks ADD COLUMN embedding vector(1536)")

    # Create indexes for common queries
    op.create_index("ix_messages_project_channel_ts", "messages", ["project_id", "slack_channel_id", "slack_message_ts"])
    op.create_index("ix_suggestions_project_status", "suggestions", ["project_id", "status"])
    op.create_index("ix_tasks_project_status", "tasks", ["project_id", "status"])
    op.create_index("ix_memory_chunks_project", "memory_chunks", ["project_id", "source_type"])


def downgrade() -> None:
    op.drop_table("memory_chunks")
    op.drop_table("clarifications")
    op.drop_table("tasks")
    op.drop_table("suggestions")
    op.drop_table("gather_runs")
    op.drop_table("messages")
    op.drop_table("processed_events")
    op.drop_table("projects")
