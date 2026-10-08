"""document ingestion

Revision ID: 007_documents
Revises: 006_event_states
Create Date: 2026-06-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "007_documents"
down_revision: Union[str, None] = "006_event_states"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_message_id", UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("slack_file_id", sa.Text(), nullable=False),
        sa.Column("slack_channel_id", sa.Text(), nullable=False),
        sa.Column("slack_user_id", sa.Text(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("mimetype", sa.Text(), nullable=True),
        sa.Column("filetype", sa.Text(), nullable=True),
        sa.Column("pretty_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("url_private", sa.Text(), nullable=True),
        sa.Column("url_private_download", sa.Text(), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("download_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("download_error", sa.Text(), nullable=True),
        sa.Column("parse_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("analysis_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("analysis_summary", sa.Text(), nullable=True),
        sa.Column("analysis_items", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "download_status IN ('pending','completed','failed','skipped')",
            name="ck_document_download_status",
        ),
        sa.CheckConstraint(
            "parse_status IN ('pending','parsed','failed','unsupported')",
            name="ck_document_parse_status",
        ),
        sa.CheckConstraint(
            "analysis_status IN ('pending','completed','failed','skipped')",
            name="ck_document_analysis_status",
        ),
    )
    op.create_index("uq_documents_slack_file_id", "documents", ["slack_file_id"], unique=True)
    op.create_index("ix_documents_project_created", "documents", ["project_id", "created_at"])
    op.create_index("ix_documents_project_download", "documents", ["project_id", "download_status"])
    op.create_index("ix_documents_project_parse", "documents", ["project_id", "parse_status"])
    op.create_index("ix_documents_project_analysis", "documents", ["project_id", "analysis_status"])

    op.create_table(
        "document_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding vector(1536)")
    op.create_index(
        "uq_document_chunks_document_index",
        "document_chunks",
        ["document_id", "chunk_index"],
        unique=True,
    )
    op.create_index(
        "ix_document_chunks_project_document",
        "document_chunks",
        ["project_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_table("document_chunks")
    op.drop_table("documents")
