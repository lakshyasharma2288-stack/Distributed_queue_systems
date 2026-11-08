"""create images

Revision ID: b7e2d41a9c53
Revises: a6c1f5e29b40
Create Date: 2026-11-07 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2d41a9c53"
down_revision: Union[str, Sequence[str], None] = "a6c1f5e29b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stored_filename", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=50), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_images"),
    )
    op.create_index(
        "uq_images_stored_filename", "images", ["stored_filename"], unique=True
    )
    op.create_index("ix_images_created_at", "images", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_images_created_at", table_name="images")
    op.drop_index("uq_images_stored_filename", table_name="images")
    op.drop_table("images")
