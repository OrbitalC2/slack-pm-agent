"""processed event states

Revision ID: 006_event_states
Revises: 005_p1_guard
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "006_event_states"
down_revision: Union[str, None] = "005_p1_guard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "processed_events",
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
    )
    op.create_check_constraint(
        "ck_processed_event_status",
        "processed_events",
        "status IN ('processing','completed','failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_processed_event_status", "processed_events", type_="check")
    op.drop_column("processed_events", "status")
