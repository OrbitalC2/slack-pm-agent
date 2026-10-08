"""phase 3 setup and schema cleanup

Revision ID: 004_phase3
Revises: 003_phase2
Create Date: 2026-06-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "004_phase3"
down_revision: Union[str, None] = "003_phase2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROJECT_TABLES = (
    "messages",
    "gather_runs",
    "suggestions",
    "tasks",
    "clarifications",
    "memory_chunks",
    "huddle_runs",
)


def _assert_no_null_project_ids(table_name: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM {table_name} WHERE project_id IS NULL) THEN
                    RAISE EXCEPTION '{table_name}.project_id contains NULL rows';
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    op.create_index(
        "uq_projects_internal_channel_id",
        "projects",
        ["internal_channel_id"],
        unique=True,
        postgresql_where=sa.text("internal_channel_id IS NOT NULL"),
    )

    for table_name in PROJECT_TABLES:
        _assert_no_null_project_ids(table_name)
        op.alter_column(table_name, "project_id", nullable=False)


def downgrade() -> None:
    for table_name in reversed(PROJECT_TABLES):
        op.alter_column(table_name, "project_id", nullable=True)

    op.drop_index("uq_projects_internal_channel_id", table_name="projects")
