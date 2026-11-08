"""add job leases

Revision ID: d4c9a771f206
Revises: 93b5ac7e12a4
Create Date: 2026-11-06 12:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4c9a771f206"
down_revision: Union[str, Sequence[str], None] = "93b5ac7e12a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(
            sa.Column("lease_owner_id", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "lease_version",
                sa.BigInteger(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_jobs_lease_owner_expiry_pair",
            "(lease_owner_id IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_jobs_status_lease_expires_at",
            ["status", "lease_expires_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index("ix_jobs_status_lease_expires_at")
        batch_op.drop_constraint("ck_jobs_lease_owner_expiry_pair", type_="check")
        batch_op.drop_column("lease_version")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner_id")
