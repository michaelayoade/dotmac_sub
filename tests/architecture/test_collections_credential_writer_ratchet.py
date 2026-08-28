"""Collections requests a credential consequence; it does not write one.

Ledger rows ``COL-R5`` and ``COL-R7`` in the Starter repository's
``docs/inventories/commercial-retirement-ledger.md``.

What is checked here:

* ``app/services/collections/**`` assigns neither RADIUS profile column, and
  the detector can still see one;
* every other assigner matches a two-directional, shrink-only baseline naming
  its real owner — so "not collections' debt" is distinguishable from "nobody
  looked";
* the four symbols COL-R7 retired have zero references anywhere, and that
  detector is sensitive too;
* the declared owners in ``collections_authority`` are the modules that
  actually hold the two halves.

The ratchet is deliberately narrower than COL-R5's row states. The row says
*"assignments … outside ``account_lifecycle``"*, which on ``origin/dev`` at
``ad3b32152`` covered eighteen sites in eight modules — only eight of them
collections', and ``account_lifecycle`` wrote neither column. That predicate
would have failed on ordinary catalog and RADIUS provisioning work. See
``app/services/collections_authority`` for the correction.
"""

from __future__ import annotations

from pathlib import Path

from app.services.collections_authority import (
    COLLECTIONS_CREDENTIAL_WRITE_SITES,
    COLLECTIONS_WRITER_ROOT,
    CONSEQUENCE_APPLIER,
    CONSEQUENCE_DECIDER,
    CREDENTIAL_PROFILE_COLUMNS,
    OUT_OF_PROGRAMME_CREDENTIAL_WRITERS,
    RETIRED_DEAD_SYMBOLS,
)
from scripts.architecture.billing_target_guards import read_count_baseline
from scripts.architecture.credential_writer_guards import (
    CREDENTIAL_PROFILE_ATTRS,
    credential_profile_write_sites,
    symbol_reference_sites,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("collections_credential_writer_baseline.txt")


def test_collections_writes_no_credential_profile() -> None:
    """COL-R5: the collections tree assigns neither profile column."""

    offenders = {
        path: count
        for path, count in credential_profile_write_sites().items()
        if path.startswith(COLLECTIONS_WRITER_ROOT)
    }

    assert sum(offenders.values()) == COLLECTIONS_CREDENTIAL_WRITE_SITES, (
        "collections assigned a credential RADIUS profile column directly. "
        f"Send a typed command to {CONSEQUENCE_DECIDER}, which revalidates and "
        f"permits or refuses, and let {CONSEQUENCE_APPLIER} perform the write:\n  "
        + "\n  ".join(f"{count} {path}" for path, count in sorted(offenders.items()))
    )


def test_the_credential_writer_detector_is_sensitive(tmp_path: Path) -> None:
    """ADR-0018 decision 5: prove the detector fails against a planted write.

    Without this, a passing ``test_collections_writes_no_credential_profile``
    and a detector that has stopped scanning are the same observation.
    """

    planted = tmp_path / "collections" / "_core.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(
        "def throttle(credential, profile_id):\n"
        "    credential.pre_throttle_radius_profile_id = credential.radius_profile_id\n"
        "    credential.radius_profile_id = profile_id\n"
        "\n"
        "def read_only(credential):\n"
        "    return credential.radius_profile_id\n",
        encoding="utf-8",
    )

    found = credential_profile_write_sites(tmp_path)

    assert found == {"collections/_core.py": 2}, (
        "the credential-writer detector did not count a planted assignment, so "
        "a clean run proves nothing about the region it claims to guard"
    )


def test_the_detector_counts_assignments_not_references(tmp_path: Path) -> None:
    """A read is a decision input, not a write, and must not be counted.

    Collections still reads both columns to build its preview. A detector that
    counted references would force that read out too, and would be measuring
    the wrong thing.
    """

    reader = tmp_path / "reader.py"
    reader.write_text(
        "def preview(credential):\n"
        "    if credential.pre_throttle_radius_profile_id is None:\n"
        "        return credential.radius_profile_id\n"
        "    return credential.pre_throttle_radius_profile_id\n",
        encoding="utf-8",
    )

    assert credential_profile_write_sites(tmp_path) == {}


def test_out_of_programme_writers_match_the_baseline() -> None:
    """Two-directional: a rise fails, and so does an unrecorded fall."""

    current = {
        path: count
        for path, count in credential_profile_write_sites().items()
        if not path.startswith(COLLECTIONS_WRITER_ROOT)
    }
    baseline = read_count_baseline(BASELINE)

    added = sorted(set(current) - set(baseline))
    assert not added, (
        "a new module assigns a credential RADIUS profile column. Route it "
        f"through {CONSEQUENCE_APPLIER}, or record it here with its owner:\n  "
        + "\n  ".join(added)
    )

    removed = sorted(set(baseline) - set(current))
    assert not removed, (
        "a recorded credential writer is gone; delete it from the shrink-only "
        "baseline in the same change:\n  " + "\n  ".join(removed)
    )

    risen = sorted(path for path in current if current[path] > baseline[path])
    assert not risen, (
        "a recorded credential writer gained assignments; the baseline only "
        "shrinks:\n  "
        + "\n  ".join(f"{path}: {baseline[path]} -> {current[path]}" for path in risen)
    )

    fallen = sorted(path for path in current if current[path] < baseline[path])
    assert not fallen, (
        "a recorded credential writer shed assignments; lower its count here "
        "in the same change so the progress is recorded rather than absorbed:\n  "
        + "\n  ".join(f"{path}: {baseline[path]} -> {current[path]}" for path in fallen)
    )


def test_every_baselined_writer_declares_an_owner() -> None:
    """A recorded writer without a named owner is an unmonitored region."""

    baseline = read_count_baseline(BASELINE)

    undeclared = sorted(set(baseline) - set(OUT_OF_PROGRAMME_CREDENTIAL_WRITERS))
    assert not undeclared, (
        "baselined credential writers with no owner declared in "
        "app/services/collections_authority.py:\n  " + "\n  ".join(undeclared)
    )

    stale = sorted(set(OUT_OF_PROGRAMME_CREDENTIAL_WRITERS) - set(baseline))
    assert not stale, (
        "declared credential-writer owners that no longer write anything; "
        "remove the declaration:\n  " + "\n  ".join(stale)
    )


def test_retired_dead_symbols_have_no_references() -> None:
    """COL-R7: the four dead symbols are gone from app/ and tests/."""

    survivors = symbol_reference_sites(
        RETIRED_DEAD_SYMBOLS,
        (PROJECT_ROOT / "app", PROJECT_ROOT / "tests"),
    )
    # This module names them in a frozenset in order to look for them, and the
    # declaration module names them for the same reason. Neither is a caller.
    expected_holders = {
        "app/services/collections_authority.py",
        "tests/architecture/test_collections_credential_writer_ratchet.py",
    }
    offenders = {
        path: count for path, count in survivors.items() if path not in expected_holders
    }

    assert not offenders, (
        "COL-R7 retired these symbols; something still references them:\n  "
        + "\n  ".join(f"{count} {path}" for path, count in sorted(offenders.items()))
    )


def test_the_retired_symbol_detector_is_sensitive(tmp_path: Path) -> None:
    """ADR-0018 decision 5, again: an empty result must be an observation."""

    revived = tmp_path / "app"
    revived.mkdir()
    (revived / "revived.py").write_text(
        "from app.services.collections._core import _throttle_account\n"
        "\n"
        "def sweep(db, account_id):\n"
        "    return _throttle_account(db, account_id)\n",
        encoding="utf-8",
    )

    # symbol_reference_sites reports paths relative to PROJECT_ROOT, so a
    # planted tree outside it cannot be addressed by name — the count is what
    # matters, and it must not be zero.
    found = symbol_reference_sites(RETIRED_DEAD_SYMBOLS, (revived,))

    assert sum(found.values()) == 2, (
        "the retired-symbol detector did not see a revived caller, so a clean "
        f"run proves nothing: {found}"
    )


def test_the_declared_owners_hold_the_two_halves() -> None:
    """The declarations name the modules that actually do the work."""

    decider = PROJECT_ROOT / f"{CONSEQUENCE_DECIDER.replace('.', '/')}.py"
    applier = PROJECT_ROOT / f"{CONSEQUENCE_APPLIER.replace('.', '/')}.py"

    assert decider.is_file(), CONSEQUENCE_DECIDER
    assert applier.is_file(), CONSEQUENCE_APPLIER

    decider_source = decider.read_text(encoding="utf-8")
    assert "def apply_credential_throttle(" in decider_source
    assert "def apply_credential_restore(" in decider_source
    assert "class CredentialConsequenceRefused(" in decider_source

    applier_source = applier.read_text(encoding="utf-8")
    assert "def apply_throttle_profile(" in applier_source
    assert "def restore_throttle_profile(" in applier_source

    # The declaration and the detector must agree on what a write is, or the
    # ratchet guards a different set of columns than the one documented.
    assert set(CREDENTIAL_PROFILE_COLUMNS) == set(CREDENTIAL_PROFILE_ATTRS)
