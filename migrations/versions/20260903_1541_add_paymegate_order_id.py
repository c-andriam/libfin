"""add_paymegate_order_id

Revision ID: ab0a7fc05f3e
Revises: f41c98b2d7e0
Created: 2026-09-03 15:41:03.784344
"""
from alembic import op
import sqlalchemy as sa


revision = 'ab0a7fc05f3e'
down_revision = 'f41c98b2d7e0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("paymegate_order_id", sa.String(length=64), nullable=True),
    )
    # Unique index (not a named constraint) so Alembic autogenerate matches the
    # model by name — sqlalchemy emits ix_<table>_<column> for index=True.
    op.create_index(
        "ix_transactions_paymegate_order_id",
        "transactions",
        ["paymegate_order_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_paymegate_order_id", table_name="transactions")
    op.drop_column("transactions", "paymegate_order_id")
