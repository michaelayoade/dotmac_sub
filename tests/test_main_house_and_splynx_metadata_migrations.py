"""Static contract for migrations 663 (house designation) and 664 (metadata purge).

The data behaviour of both migrations is PostgreSQL-only and is proven in
``tests/integration/test_main_house_and_splynx_metadata_migrations.py``. This
file pins what can be checked without a database: the revision chain, the
non-PostgreSQL no-op, the key lists, and the irreversible downgrade.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.services.subscriber_metadata_keys import DECLARED_METADATA_KEYS
from scripts.architecture.subscriber_metadata_census import RETIRED_SPLYNX_KEYS

ROOT = Path(__file__).resolve().parents[1]
HOUSE_MIGRATION = ROOT / "alembic/versions/663_main_canonical_house_reseller.py"
PURGE_MIGRATION = ROOT / "alembic/versions/664_purge_retired_splynx_metadata_keys.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revisions_chain_onto_the_main_trunk_head() -> None:
    house = _load(HOUSE_MIGRATION, "main_canonical_house_reseller")
    purge = _load(PURGE_MIGRATION, "purge_retired_splynx_metadata_keys")
    assert house.revision == "663_main_canonical_house_reseller"
    assert house.down_revision == "662_validate_regional_report_billing_indexes"
    assert purge.revision == "664_purge_retired_splynx_metadata_keys"
    assert purge.down_revision == house.revision

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("version_locations", str(ROOT / "alembic/versions"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    ancestry = {
        item.revision
        for item in script.iterate_revisions(heads[0], house.revision, inclusive=True)
    }
    assert {house.revision, purge.revision} <= ancestry


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (HOUSE_MIGRATION, "main_canonical_house_reseller"),
        (PURGE_MIGRATION, "purge_retired_splynx_metadata_keys"),
    ],
)
def test_non_postgres_databases_are_left_untouched(
    path: Path, name: str, monkeypatch
) -> None:
    """Both migrations use PostgreSQL-only SQL and must no-op elsewhere."""

    module = _load(path, name)

    class _NonPostgresBind:
        """A bind that fails the test if the migration issues any statement."""

        dialect = SimpleNamespace(name="sqlite")

        def execute(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("a non-PostgreSQL bind must not be touched")

    monkeypatch.setattr(
        module, "op", SimpleNamespace(get_bind=lambda: _NonPostgresBind())
    )
    module.upgrade()
    module.downgrade()


def test_purge_list_is_exactly_the_reviewed_retired_keys() -> None:
    purge = _load(PURGE_MIGRATION, "purge_retired_splynx_metadata_keys")
    assert purge.RETIRED_KEYS == RETIRED_SPLYNX_KEYS
    assert len(set(purge.RETIRED_KEYS)) == len(purge.RETIRED_KEYS)
    assert "splynx_password_cleartext" in purge.RETIRED_KEYS


def test_purge_never_touches_declared_or_still_read_keys() -> None:
    purge = _load(PURGE_MIGRATION, "purge_retired_splynx_metadata_keys")
    retired = set(purge.RETIRED_KEYS)
    assert not retired & set(DECLARED_METADATA_KEYS)
    still_read = (ROOT / "app/services/web_subscriber_details.py").read_text(
        encoding="utf-8"
    )
    for key in retired:
        assert f'"{key}"' not in still_read and f"'{key}'" not in still_read
    # Read by the subscriber detail enrichment; must survive the purge.
    for key in (
        "splynx_last_online",
        "splynx_gps",
        "splynx_location_id",
    ):
        assert f'"{key}"' in still_read
        assert key not in retired
    # Moved to billing contacts by 665 and no longer read, but conflict and
    # invalid rows keep it, so 664 must still not purge it.
    assert '"splynx_billing_email"' not in still_read
    assert "splynx_billing_email" not in retired


def test_purge_downgrade_is_an_explicit_irreversible_no_op() -> None:
    purge = _load(PURGE_MIGRATION, "purge_retired_splynx_metadata_keys")
    assert "Irreversible" in (purge.__doc__ or "")
    assert purge.downgrade() is None


def test_purge_copies_no_values_into_the_audit_row() -> None:
    """The audit payload names keys and counts only."""

    source = PURGE_MIGRATION.read_text(encoding="utf-8")
    payload = source[source.index('"metadata": json.dumps(') :]
    payload = payload[: payload.index("sort_keys=True")]
    for field in ('"keys"', '"row_counts"', '"rows_rewritten"', '"reason"'):
        assert field in payload
    assert "metadata::jsonb ->" not in source
    assert "->>" not in source
