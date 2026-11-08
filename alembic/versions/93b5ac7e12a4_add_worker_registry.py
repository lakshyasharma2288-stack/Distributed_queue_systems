"""add worker registry

Revision ID: 93b5ac7e12a4
Revises: ac8fd6d77183
Create Date: 2026-11-06 12:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "93b5ac7e12a4"
down_revision: Union[str, Sequence[str], None] = "ac8fd6d77183"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="starting",
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_job_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('starting', 'active', 'draining', 'stopped', 'dead')",
            name="ck_workers_status",
        ),
        sa.ForeignKeyConstraint(["current_job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_workers_status_heartbeat",
        "workers",
        ["status", "last_heartbeat_at"],
    )
    op.create_index(
        "ix_workers_current_job_id",
        "workers",
        ["current_job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_workers_current_job_id", table_name="workers")
    op.drop_index("ix_workers_status_heartbeat", table_name="workers")
    op.drop_table("workers")
