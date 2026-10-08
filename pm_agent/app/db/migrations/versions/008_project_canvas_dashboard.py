"""project canvas dashboard

Revision ID: 008_canvas_dashboard
Revises: 007_documents
Create Date: 2026-06-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "008_canvas_dashboard"
down_revision: Union[str, None] = "007_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("internal_canvas_id", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("internal_canvas_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "projects",
        sa.Column(
            "internal_canvas_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'not_created'"),
        ),
    )
    op.add_column(
        "projects", sa.Column("latest_client_update_draft", sa.Text(), nullable=True)
    )
    op.add_column(
        "projects",
        sa.Column("latest_client_update_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_project_internal_canvas_status",
        "projects",
        "internal_canvas_status IN ('not_created','created','failed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_project_internal_canvas_status", "projects", type_="check"
    )
    op.drop_column("projects", "latest_client_update_generated_at")
    op.drop_column("projects", "latest_client_update_draft")
    op.drop_column("projects", "internal_canvas_status")
    op.drop_column("projects", "internal_canvas_updated_at")
    op.drop_column("projects", "internal_canvas_id")
