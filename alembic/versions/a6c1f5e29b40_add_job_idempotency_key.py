"""add job idempotency key

Revision ID: a6c1f5e29b40
Revises: 981a07d4c2b9
Create Date: 2026-11-06 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6c1f5e29b40"
down_revision: Union[str, Sequence[str], None] = "981a07d4c2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("idempotency_key", sa.String(length=255)))
    op.create_index(
        "uq_jobs_idempotency_key",
        "jobs",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_idempotency_key", table_name="jobs")
    op.drop_column("jobs", "idempotency_key")
