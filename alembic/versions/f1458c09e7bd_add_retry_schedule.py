"""add retry schedule

Revision ID: f1458c09e7bd
Revises: d4c9a771f206
Create Date: 2026-11-06 13:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1458c09e7bd"
down_revision: Union[str, Sequence[str], None] = "d4c9a771f206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_jobs_status_next_retry_at",
        "jobs",
        ["status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_status_next_retry_at", table_name="jobs")
    op.drop_column("jobs", "next_retry_at")
