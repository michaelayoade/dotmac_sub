"""`subscribers.metadata` has no owner, and this is the guard that shrinks it.

The column is a JSONB blob with no declared shape and no declared owner. Eight
modules write it, thirteen more read it, and its keys accumulated because a
feature needed somewhere to put something and nothing said no.

The ratchet here is deliberately **membership only**, unlike the cohort writer
ratchet which also counts sites. A module either writes this column or it does
not; how many lines it takes to do so says nothing about ownership, and a
magnitude baseline would invite lowering the count by consolidating writes into
one line rather than by giving the fact an owner.

Every check pairs with a sensitivity case. A guard nobody has watched fail is a
guard nobody should trust, and three of these were rewritten precisely because
the first version passed against code it should have caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture.subscriber_metadata_census import (
    RETIRED_SPLYNX_KEYS,
    Access,
    _Census,
    _string_constants,
    keys_by_module,
    metadata_readers,
    metadata_writers,
    render_writer_baseline,
    retired_key_references,
    unclassified_receivers,
)

BASELINE = Path(__file__).with_name("subscriber_metadata_writers_baseline.txt")
PURGE_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/664_purge_retired_splynx_metadata_keys.py"
)

REGENERATE = (
    "Regenerate with `python -m scripts.architecture.subscriber_metadata_census "
    "--baseline` in the same change."
)


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _parse(source: str) -> _Census:
    import ast

    tree = ast.parse(source)
    census = _Census("test_module.py", _string_constants(tree))
    census.visit(tree)
    census.accesses.clear()
    census.visit(tree)
    return census


# --------------------------------------------------------------------------
# the two-directional membership ratchet
# --------------------------------------------------------------------------


def test_no_new_module_writes_subscriber_metadata() -> None:
    added = sorted(metadata_writers() - _baseline())
    assert not added, (
        "these modules write `subscribers.metadata` and the baseline does not "
        "list them. The column has no owner and no shape, so a new writer is a "
        "new unowned fact rather than a new feature:\n  "
        + "\n  ".join(added)
        + f"\n{REGENERATE}"
    )


def test_the_writer_baseline_is_not_stale() -> None:
    """The other direction, and the one that makes progress countable.

    A module that stopped writing the column while the baseline still claims it
    means the ratchet can be spent twice: the next module to start writing
    would take the freed slot and the count would look unchanged.
    """

    retired = sorted(_baseline() - metadata_writers())
    assert not retired, (
        "these modules no longer write `subscribers.metadata` and the baseline "
        "still lists them. Delete each line in the change that routed the fact "
        "through its owner:\n  " + "\n  ".join(retired) + f"\n{REGENERATE}"
    )


def test_the_generated_baseline_matches_the_checked_in_one() -> None:
    assert render_writer_baseline() == BASELINE.read_text(encoding="utf-8"), REGENERATE


# --------------------------------------------------------------------------
# the census must be able to see a writer at all
# --------------------------------------------------------------------------


def test_every_receiver_is_classified() -> None:
    """The load-bearing half.

    A writer behind a receiver the census cannot resolve escapes every other
    check here. Resolution is by binding — annotation, construction,
    `db.get(Model, ...)`, a query terminal, or a same-name function's return
    annotation — never by variable name, because half this codebase's receivers
    are called `target`, `existing` or `record` and each names a different model
    in a different module.
    """

    unresolved = unclassified_receivers()
    assert not unresolved, (
        "these `<name>.metadata_` receivers resolve to no model. Each is a "
        "possible unseen writer of the subscriber column. Annotate the binding, "
        "or add the name to REVIEWED_FOREIGN_RECEIVERS with the reading that "
        "proves it is another model:\n  " + "\n  ".join(unresolved)
    )


def test_the_census_detects_a_new_writer_sensitivity() -> None:
    """The plain copy-mutate-reassign shape, which most writers use."""

    census = _parse(
        "from app.models.subscriber import Subscriber\n"
        "def handler(subscriber: Subscriber) -> None:\n"
        "    meta = dict(subscriber.metadata_ or {})\n"
        "    meta['invented_key'] = 1\n"
        "    subscriber.metadata_ = meta\n"
    )
    assert Access("test_module.py", "write", "invented_key") in census.accesses


def test_the_census_detects_a_deletion_sensitivity() -> None:
    census = _parse(
        "from app.models.subscriber import Subscriber\n"
        "def handler(subscriber: Subscriber) -> None:\n"
        "    meta = dict(subscriber.metadata_ or {})\n"
        "    meta.pop('invented_key', None)\n"
        "    subscriber.metadata_ = meta\n"
    )
    assert Access("test_module.py", "delete", "invented_key") in census.accesses


def test_a_constant_key_is_resolved_sensitivity() -> None:
    """Most writers name their key through a module constant, not a literal."""

    census = _parse(
        "from app.models.subscriber import Subscriber\n"
        "_KEY = 'resolved_through_a_constant'\n"
        "def handler(subscriber: Subscriber) -> None:\n"
        "    meta = dict(subscriber.metadata_ or {})\n"
        "    meta[_KEY] = 1\n"
        "    subscriber.metadata_ = meta\n"
    )
    assert (
        Access("test_module.py", "write", "resolved_through_a_constant")
        in census.accesses
    )


def test_a_foreign_model_blob_is_not_counted_sensitivity() -> None:
    """`location_request.metadata_` is a different column on a different table.

    The first draft of this census matched on the attribute name alone and
    reported `auto_decision` — a `CustomerLocationChangeRequest` key — as a
    subscriber fact. Over-reporting is not the safe direction here: it would
    have sent someone to find an owner for a fact that already has one.
    """

    census = _parse(
        "from app.models.gis import CustomerLocationChangeRequest\n"
        "from app.models.subscriber import Subscriber\n"
        "def handler(location_request: CustomerLocationChangeRequest) -> None:\n"
        "    location_request.metadata_ = {'auto_decision': True}\n"
    )
    assert not [
        access for access in census.accesses if access.key == "auto_decision"
    ], "a foreign model's blob was counted as a subscriber fact"


def test_an_unresolvable_receiver_is_reported_not_skipped_sensitivity() -> None:
    census = _parse(
        "from app.models.subscriber import Subscriber\n"
        "def handler(mystery) -> None:\n"
        "    mystery.metadata_ = {'unseen': 1}\n"
    )
    assert any("mystery" in entry for entry in census.unclassified), (
        "an unresolvable receiver was silently skipped; that is exactly how a "
        "writer stays invisible"
    )


# --------------------------------------------------------------------------
# facts about the column that the ownership work has to move
# --------------------------------------------------------------------------


def test_no_writer_stores_a_key_it_computes_at_runtime() -> None:
    """`<dynamic>` means the module has no declarable shape at all.

    A key computed at runtime cannot be assigned an owner, migrated with a
    typed contract, or even listed. This currently passes, and the value of the
    check is prospective: it stops the next writer from reaching for a
    computed key while the existing ones are being retired.
    """

    dynamic = sorted(
        module
        for module, operations in keys_by_module().items()
        for operation, keys in operations.items()
        if operation in {"write", "delete"} and "<dynamic>" in keys
    )
    assert not dynamic, (
        "these modules write metadata keys this census cannot resolve to a "
        "literal, so the column's shape is not merely undeclared but "
        "undeclarable:\n  " + "\n  ".join(dynamic)
    )


def test_readers_outnumber_writers() -> None:
    """Shape, not a number.

    Retiring a writer is a bounded change; retiring a reader means finding
    everything that consumes a projection. If these ever converged the
    compatibility-projection plan would be measuring the wrong risk.
    """

    assert len(metadata_readers()) > len(metadata_writers())


def test_every_written_key_is_declared_in_the_registry() -> None:
    """The census and the closed key registry must not drift apart.

    The registry is what refuses an undeclared key at the owner. A key a module
    actually writes but nobody declared would be refused in production while
    passing every test that does not exercise that path — a guard that breaks
    working code rather than catching a new one.
    """

    from app.services.subscriber_metadata_keys import DECLARED_METADATA_KEYS

    written = {
        key
        for operations in keys_by_module().values()
        for operation, keys in operations.items()
        if operation in {"write", "delete"}
        for key in keys
        if key != "<dynamic>"
    }
    undeclared = sorted(written - set(DECLARED_METADATA_KEYS))
    assert not undeclared, (
        "these keys are written to `subscribers.metadata` and the closed "
        "registry does not declare them, so the owner would refuse a write the "
        "code still makes:\n  " + "\n  ".join(undeclared)
    )


def test_the_registry_declares_no_key_nothing_uses() -> None:
    """The other direction: a declared key with no accessor is dead permission.

    It would let a caller write a key no service reads, which is how the column
    grew in the first place. Read access counts — several keys are written by a
    service and read by a projection, and some are now read-only provenance.
    """

    from app.services.subscriber_metadata_keys import DECLARED_METADATA_KEYS

    accessed = {
        key
        for operations in keys_by_module().values()
        for keys in operations.values()
        for key in keys
    }
    orphaned = sorted(set(DECLARED_METADATA_KEYS) - accessed)
    assert not orphaned, (
        "these keys are declared writable and nothing reads or writes them. A "
        "declared key with no accessor is permission to grow the column "
        "again:\n  " + "\n  ".join(orphaned)
    )


@pytest.mark.parametrize(
    "key",
    [
        # A JSON copy of `Subscriber.category`, a real typed column, read by
        # nine modules. Obsolete by construction rather than by decision.
        "subscriber_category",
        # A fourth place an address coordinate lives, after Address.latitude,
        # Address.geom and GeoLocation. `gis.spatial_sync` owns coordinates.
        "latitude",
        "longitude",
    ],
)
def test_duplicate_column_projections_are_still_present(key: str) -> None:
    """These are recorded as present so their removal is a visible event.

    Each duplicates a fact that already has a typed home. The test asserts the
    CURRENT state; when the projection is deleted this test fails and is
    deleted with it, which is the point — a silent removal would leave the
    ownership document claiming work that nobody can date.

    """

    present = {
        module
        for module, operations in keys_by_module().items()
        for keys in operations.values()
        if key in keys
    }
    assert present, (
        f"{key!r} no longer appears in `subscribers.metadata`. If it was "
        "retired, delete this parametrisation and the matching row in "
        "docs/SUBSCRIBER_METADATA_OWNERSHIP.md in the same change."
    )


# --------------------------------------------------------------------------
# keys purged by migration 664 stay purged
# --------------------------------------------------------------------------


def _purge_migration_keys() -> tuple[str, ...]:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "purge_retired_splynx_metadata_keys", PURGE_MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.RETIRED_KEYS)


def test_retired_key_list_matches_the_purge_migration() -> None:
    """One list, two copies: the migration cannot import app/scripts code."""

    assert RETIRED_SPLYNX_KEYS == _purge_migration_keys()


def test_retired_splynx_keys_are_not_declared() -> None:
    """Declaring a purged key would let the owner accept it again."""

    from app.services.subscriber_metadata_keys import DECLARED_METADATA_KEYS

    redeclared = sorted(set(RETIRED_SPLYNX_KEYS) & set(DECLARED_METADATA_KEYS))
    assert not redeclared, (
        "these keys were purged from every row by migration "
        "664_purge_retired_splynx_metadata_keys and must not be declared "
        "again:\n  " + "\n  ".join(redeclared)
    )


def test_no_product_code_references_a_retired_splynx_key() -> None:
    """A reader of a purged key would read absence forever; a writer recreates it."""

    references = retired_key_references()
    assert not references, (
        "these files reference `subscribers.metadata` keys purged by migration "
        "664_purge_retired_splynx_metadata_keys. The values no longer exist; "
        "a new fact belongs in a typed column with an owner:\n  "
        + "\n  ".join(f"{path}: {key}" for path, key in references)
    )


def test_no_retired_splynx_key_is_accessed_per_the_census() -> None:
    accessed = sorted(
        {
            key
            for operations in keys_by_module().values()
            for keys in operations.values()
            for key in keys
        }
        & set(RETIRED_SPLYNX_KEYS)
    )
    assert not accessed, accessed


def test_still_read_splynx_keys_are_not_retired() -> None:
    """Keys a reader still consumes must survive the purge."""

    still_read = {
        "splynx_date_add",
        "splynx_last_update",
        "splynx_deleted",
        "splynx_status",
        "splynx_last_online",
        "splynx_gps",
        "splynx_location_id",
        "splynx_billing_email",
    }
    assert not still_read & set(RETIRED_SPLYNX_KEYS)
    # And the scan really sees the readers that keep them alive.
    seen = {key for _, key in retired_key_references(tuple(sorted(still_read)))}
    assert {"splynx_last_online", "splynx_gps", "splynx_location_id"} <= seen
    assert "splynx_billing_email" in seen


def test_the_retired_key_scan_detects_a_reference_sensitivity(
    tmp_path, monkeypatch
) -> None:
    from scripts.architecture import subscriber_metadata_census as census

    probe = tmp_path / "app" / "probe.py"
    probe.parent.mkdir()
    probe.write_text("value = metadata.get('splynx_login')\n", encoding="utf-8")
    monkeypatch.setattr(census, "REPOSITORY_ROOT", tmp_path)
    assert census.retired_key_references() == [("app/probe.py", "splynx_login")]
