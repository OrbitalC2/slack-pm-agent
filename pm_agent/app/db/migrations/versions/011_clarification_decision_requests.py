"""clarification decision requests

Revision ID: 011_clar_decisions
Revises: 010_doc_memory
Create Date: 2026-06-23

Adds structured decision-request fields to clarifications: a JSONB payload for
options/recommended_option/why_it_matters/known_context/answer_owner/confidence/
submitter_slack_id, plus first-class answer-lifecycle columns used by the
"answer internally" flow and the dev-facing Canvas Q&A section.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "011_clar_decisions"
down_revision: Union[str, None] = "010_doc_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clarifications",
        sa.Column(
            "payload",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "clarifications", sa.Column("final_answer", sa.Text(), nullable=True)
    )
    op.add_column(
        "clarifications",
        sa.Column("answered_by_slack_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "clarifications",
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clarifications", "answered_at")
    op.drop_column("clarifications", "answered_by_slack_id")
    op.drop_column("clarifications", "final_answer")
    op.drop_column("clarifications", "payload")
