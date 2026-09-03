"""Rejoin the two migration branches that a merge left side by side.

Two lines of work both descended from ``5e1636c9c627`` and neither knew about
the other: payment links (``a1c7e2f04b58`` → ``c8b3f5d71e29``) and prestataires
(``f41c98b2d7e0`` → ``ab0a7fc05f3e``). Alembic will not upgrade to "head" when
there is more than one, so the effect was not a conflict but a stop: *no*
migration could be applied at all, and the prestataire tables were simply
missing while the API asked for them and returned 500.

A merge revision is the right repair rather than rewriting either branch's
``down_revision``. This database already has the links branch applied;
re-parenting it would make the recorded history disagree with what was actually
run, which is the one thing a migration graph must never do.

There is nothing to execute here. The two branches touch different tables — the
links branch only ``payment_links``, the prestataire branch only ``merchants``
and ``liquidity_providers`` — so joining them needs no reconciliation, and this
revision exists solely to give the graph a single head again.

Revision ID: 6d2a1b4e8f37
Revises: c8b3f5d71e29, ab0a7fc05f3e
"""

revision = "6d2a1b4e8f37"
down_revision = ("c8b3f5d71e29", "ab0a7fc05f3e")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Nothing to do: the branches are disjoint."""


def downgrade() -> None:
    """Splitting the head again would recreate the failure this repairs."""
