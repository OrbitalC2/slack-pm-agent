"""phase 2 source tracking and huddle provenance

Revision ID: 003_phase2
Revises: 002_phase1
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision: str = "003_phase2"
down_revision: Union[str, None] = "002_phase1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "messages",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    for table_name in ("suggestions", "tasks", "clarifications"):
        op.add_column(
            table_name,
            sa.Column("source_refs", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        )

    op.create_table(
        "huddle_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_id", sa.Text(), nullable=False),
        sa.Column("submitted_by_slack_id", sa.Text(), nullable=False),
        sa.Column("raw_notes", sa.Text(), nullable=False),
        sa.Column("participants", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_huddle_runs_project_created", "huddle_runs", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_huddle_runs_project_created", table_name="huddle_runs")
    op.drop_table("huddle_runs")

    for table_name in ("clarifications", "tasks", "suggestions"):
        op.drop_column(table_name, "source_refs")

    op.drop_column("messages", "updated_at")
    op.drop_column("messages", "is_deleted")
