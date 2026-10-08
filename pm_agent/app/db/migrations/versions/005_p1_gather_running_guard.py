"""p1 gather running guard

Revision ID: 005_p1_guard
Revises: 004_phase3
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "005_p1_guard"
down_revision: Union[str, None] = "004_phase3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_gather_runs_running_project_channel",
        "gather_runs",
        ["project_id", "slack_channel_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_gather_runs_running_project_channel", table_name="gather_runs")
