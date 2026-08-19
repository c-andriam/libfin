"""Add the authorise-then-capture states.

Introduces FIAT_AUTHORIZED, FIAT_CAPTURED and AUTH_VOIDED so the gateway can
hold funds, deliver the crypto, and only then take the money.

Autogenerate does not see this change: Alembic compares tables and columns, and
adding a value to a PostgreSQL enum is neither. Written by hand, which is the
normal case for enum evolution and the reason `migrate --check` alone is not
sufficient review.

Revision ID: 7a2c4f9b1d33
Revises: 5e11e7026cb2
Created: 2026-08-18
"""
from alembic import op
import sqlalchemy as sa

revision = "7a2c4f9b1d33"
down_revision = "5e11e7026cb2"
branch_labels = None
depends_on = None

NEW_STATES = ("FIAT_AUTHORIZED", "FIAT_CAPTURED", "AUTH_VOIDED")
ENUM_NAME = "transactionstatus"


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name != "postgresql":
        # SQLite stores enums as plain strings with a CHECK constraint that
        # SQLAlchemy applies at the application layer, so there is nothing to
        # alter. Used by the test suite.
        return

    # ALTER TYPE ... ADD VALUE cannot be used in the same transaction that then
    # reads the new value. Committing first keeps the type usable straight away.
    existing = {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
            ),
            {"name": ENUM_NAME},
        )
    }

    with op.get_context().autocommit_block():
        for state in NEW_STATES:
            if state not in existing:
                op.execute(f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{state}'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum. Undoing this means
    # recreating the type and rewriting every row that references it, which on a
    # table holding live payments is a migration of its own — and only safe once
    # no row uses the states being dropped. Deliberately not attempted here.
    raise NotImplementedError(
        "Removing enum values requires recreating the type and rewriting the "
        "column. Write that migration deliberately, after confirming no "
        "transaction is in FIAT_AUTHORIZED, FIAT_CAPTURED or AUTH_VOIDED."
    )
