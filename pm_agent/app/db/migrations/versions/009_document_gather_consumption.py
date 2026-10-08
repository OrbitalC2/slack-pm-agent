"""document gather consumption tracking

Revision ID: 009_doc_consumption
Revises: 008_canvas_dashboard
Create Date: 2026-06-22

Adds tracking so a document whose analysis completes after its upload message
was already gathered can still be included exactly once in a future gather run.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "009_doc_consumption"
down_revision: Union[str, None] = "008_canvas_dashboard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("analysis_consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("last_included_in_gather_run_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_documents_last_gather_run",
        "documents",
        "gather_runs",
        ["last_included_in_gather_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_documents_channel_consumed",
        "documents",
        ["project_id", "slack_channel_id", "analysis_consumed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_channel_consumed", table_name="documents")
    op.drop_constraint(
        "fk_documents_last_gather_run", "documents", type_="foreignkey"
    )
    op.drop_column("documents", "last_included_in_gather_run_id")
    op.drop_column("documents", "analysis_consumed_at")
