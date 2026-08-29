"""Payment links become managed objects rather than one-shot tokens.

Three changes, all driven by how a merchant actually uses a link. It should
last until they retire it, not until an hour is up. It should be payable more
than once, because a link is a price list entry as often as it is an invoice.
And retiring it should be reversible — off is not the same as gone.

Revision ID: c8b3f5d71e29
Revises: a1c7e2f04b58
"""

import sqlalchemy as sa
from alembic import op

revision = "c8b3f5d71e29"
down_revision = "a1c7e2f04b58"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default on both: the table may already hold rows, and a NOT NULL
    # column without one cannot be added to a populated table.
    op.add_column(
        "payment_links",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "payment_links",
        sa.Column("payment_count", sa.Integer(), nullable=False, server_default="0"),
    )

    # Existing rows were single-use, so anything already used has been paid
    # exactly once. Saying nothing would report those links as never paid.
    op.execute(
        "UPDATE payment_links SET payment_count = 1 WHERE used_at IS NOT NULL"
    )

    # SQLite cannot ALTER a column; the tests run there and create the schema
    # from the model, which already has the nullable form.
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column("payment_links", "expires_at", nullable=True)


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        # Rows with no expiry cannot go back to a NOT NULL column; give them
        # one rather than failing the downgrade.
        op.execute(
            "UPDATE payment_links SET expires_at = CURRENT_TIMESTAMP "
            "WHERE expires_at IS NULL"
        )
        op.alter_column("payment_links", "expires_at", nullable=False)
    op.drop_column("payment_links", "payment_count")
    op.drop_column("payment_links", "active")
