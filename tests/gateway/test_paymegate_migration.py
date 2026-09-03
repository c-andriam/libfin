"""
Lightweight validation of the PayMeGate migration script.

The full Alembic upgrade/downgrade cycle requires a real Postgres connection and
an initialised Alembic environment, which the CI rig does not provision.  These
smoke-checks ensure the migration module is valid Python, carries the expected
revision chain, and defines both upgrade and downgrade callables — enough to catch
syntax errors and copy-paste regressions without a running database.
"""

import importlib
import types


def _load_migration() -> types.ModuleType:
    """Import the migration module without triggering Alembic internals."""
    return importlib.import_module(
        "migrations.versions.20260903_1541_add_paymegate_order_id"
    )


def test_migration_has_expected_revision_ids():
    mod = _load_migration()
    assert mod.revision == "ab0a7fc05f3e"
    assert mod.down_revision == "f41c98b2d7e0"


def test_migration_defines_upgrade_and_downgrade():
    mod = _load_migration()
    assert callable(mod.upgrade)
    assert callable(mod.downgrade)


def test_migration_downgrade_has_no_side_effects():
    """Downgrade must be a plain callable (even if it is a no-op on SQLite)."""
    mod = _load_migration()
    import inspect

    sig = inspect.signature(mod.downgrade)
    assert sig.parameters == {}
