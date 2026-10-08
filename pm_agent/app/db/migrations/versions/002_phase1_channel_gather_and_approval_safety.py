"""phase 1 channel gather and approval safety

Revision ID: 002_phase1
Revises: 001_initial
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "002_phase1"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Gather runs become channel-scoped. Add nullable first, backfill, then enforce.
    op.add_column("gather_runs", sa.Column("slack_channel_id", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE gather_runs AS gr
        SET slack_channel_id = p.client_channel_id
        FROM projects AS p
        WHERE gr.project_id = p.id
          AND gr.slack_channel_id IS NULL
        """
    )
    op.alter_column("gather_runs", "slack_channel_id", nullable=False)

    op.add_column(
        "gather_runs",
        sa.Column(
            "notification_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'not_required'"),
        ),
    )
    op.create_check_constraint(
        "ck_gather_notification_status",
        "gather_runs",
        "notification_status IN ('not_required','pending','sent','failed')",
    )

    op.create_index(
        "uq_tasks_source_suggestion_id",
        "tasks",
        ["source_suggestion_id"],
        unique=True,
        postgresql_where=sa.text("source_suggestion_id IS NOT NULL"),
    )
    op.create_index(
        "uq_clarifications_source_suggestion_id",
        "clarifications",
        ["source_suggestion_id"],
        unique=True,
        postgresql_where=sa.text("source_suggestion_id IS NOT NULL"),
    )

    # Acknowledged scope risks and assumptions are persisted as trusted memory.
    op.drop_constraint("ck_memory_source_type", "memory_chunks", type_="check")
    op.create_check_constraint(
        "ck_memory_source_type",
        "memory_chunks",
        "source_type IN ('gather_summary','huddle_summary','decision','scope_risk','assumption','task','clarification','github_summary','manual_note')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_source_type", "memory_chunks", type_="check")
    op.create_check_constraint(
        "ck_memory_source_type",
        "memory_chunks",
        "source_type IN ('gather_summary','huddle_summary','decision','task','clarification','github_summary','manual_note')",
    )

    op.drop_index("uq_clarifications_source_suggestion_id", table_name="clarifications")
    op.drop_index("uq_tasks_source_suggestion_id", table_name="tasks")
    op.drop_constraint("ck_gather_notification_status", "gather_runs", type_="check")
    op.drop_column("gather_runs", "notification_status")
    op.drop_column("gather_runs", "slack_channel_id")
