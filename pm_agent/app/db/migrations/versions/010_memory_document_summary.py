"""allow document_summary memory source type

Revision ID: 010_doc_memory
Revises: 009_doc_consumption
Create Date: 2026-06-22

Extends the memory_chunks source_type check constraint so project memory can be
created from completed document analyses (source_type='document_summary').

"""
from typing import Sequence, Union

from alembic import op


revision: str = "010_doc_memory"
down_revision: Union[str, None] = "009_doc_consumption"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_SOURCE_TYPES = (
    "gather_summary',"
    "'huddle_summary',"
    "'decision',"
    "'scope_risk',"
    "'assumption',"
    "'task',"
    "'clarification',"
    "'github_summary',"
    "'manual_note',"
    "'document_summary"
)

_OLD_SOURCE_TYPES = (
    "gather_summary',"
    "'huddle_summary',"
    "'decision',"
    "'scope_risk',"
    "'assumption',"
    "'task',"
    "'clarification',"
    "'github_summary',"
    "'manual_note"
)


def upgrade() -> None:
    op.drop_constraint("ck_memory_source_type", "memory_chunks", type_="check")
    op.create_check_constraint(
        "ck_memory_source_type",
        "memory_chunks",
        f"source_type IN ('{_NEW_SOURCE_TYPES}')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_memory_source_type", "memory_chunks", type_="check")
    op.create_check_constraint(
        "ck_memory_source_type",
        "memory_chunks",
        f"source_type IN ('{_OLD_SOURCE_TYPES}')",
    )
