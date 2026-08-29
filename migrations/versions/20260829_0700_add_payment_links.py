"""Add payment links: an order named by a token instead of carried in a URL.

A 2D link used to put the amount and the destination wallet in the query
string. Anyone forwarded the link could read them, and — the part that matters
— rewrite them before paying. This table moves the order server-side so the
link carries only an opaque token, and nothing the payer sends can change what
they are charged or where the crypto goes.

Revision ID: a1c7e2f04b58
Revises: 5e1636c9c627
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c7e2f04b58"
down_revision = "5e1636c9c627"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Long enough for token_urlsafe(24) with room to grow the entropy later
        # without another migration.
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("target_wallet", sa.String(length=42), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
    )
    # Unique, not merely indexed: a duplicate token would silently hand one
    # merchant's order to another's payer. Let the database refuse it.
    op.create_index(
        "ix_payment_links_token", "payment_links", ["token"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_payment_links_token", table_name="payment_links")
    op.drop_table("payment_links")
