"""add attempts and dead letters

Revision ID: 981a07d4c2b9
Revises: f1458c09e7bd
Create Date: 2026-11-06 13:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "981a07d4c2b9"
down_revision: Union[str, Sequence[str], None] = "f1458c09e7bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "job_attempts",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.String(length=30),
            server_default="running",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('running', 'completed', 'retry_scheduled', 'dead')",
            name="ck_job_attempts_outcome",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("job_id", "attempt_number"),
    )
    op.create_index(
        "ix_job_attempts_worker_started",
        "job_attempts",
        ["worker_id", "started_at"],
    )
    op.create_table(
        "dead_letters",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("final_error_code", sa.String(length=100), nullable=False),
        sa.Column("final_error_message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("job_id"),
    )


def downgrade() -> None:
    op.drop_table("dead_letters")
    op.drop_index("ix_job_attempts_worker_started", table_name="job_attempts")
    op.drop_table("job_attempts")
