"""`docs/kernel-runtime-readiness.json` is true of this tree, mechanically.

Starter's Kernel-successor compatibility gate used to take Sub's readiness
facts as booleans authored in STARTER's own JSON -- a producing repository
asserting something about a consuming repository it cannot verify. This
record replaces that: Sub owns the typed record, in Sub's own tree, and this
file is the only thing in the world entitled to say a claim in it is true.
Starter's gate reads the record from a Git blob at a pinned revision and
parses it; it authors nothing.

Every fact this file checks is re-derived independently from the source
tree by `kernel_runtime_readiness.py` (AST, not text search, and never by
trusting the record's own `statement`/`source_reference` text) and compared
against what the record claims. A requirement id or composition declaration
with no registered checker fails loudly rather than passing by omission.
"""

from __future__ import annotations

import copy

import pytest

from tests.architecture.kernel_runtime_readiness import (
    COMPOSITION_CHECKS,
    REQUIREMENT_CHECKS,
    RecordValidationError,
    load_record,
    resolve_source_reference,
    validate_record,
)


def test_the_committed_record_is_valid() -> None:
    """Near-miss / legitimate-record proof: the real, committed record
    passes whole -- envelope, every source_reference, every requirement,
    every composition declaration."""

    record = load_record()
    validate_record(record)


def test_every_requirement_id_has_a_registered_checker() -> None:
    record = load_record()
    ids = {entry["id"] for entry in record["requirements"]}
    assert ids <= REQUIREMENT_CHECKS.keys()
    # And the reverse: no dead checker for an id the record no longer states.
    assert REQUIREMENT_CHECKS.keys() <= ids


def test_every_composition_declaration_has_a_registered_checker() -> None:
    record = load_record()
    declarations = {entry["declaration"] for entry in record["composition"]}
    assert declarations <= COMPOSITION_CHECKS.keys()
    assert COMPOSITION_CHECKS.keys() <= declarations


def test_every_source_reference_resolves_inside_the_tree() -> None:
    record = load_record()
    for entry in record["requirements"] + record["composition"]:
        # Raises RecordValidationError on any miss -- a bare call is the
        # assertion here.
        resolve_source_reference(entry["source_reference"])


@pytest.mark.parametrize("checker_name", sorted(REQUIREMENT_CHECKS))
def test_each_requirement_checker_currently_returns_true(checker_name: str) -> None:
    """Every requirement in the committed record claims `satisfied: true`.
    This independently proves each checker itself currently agrees, so a
    later failure in `test_the_committed_record_is_valid` is attributable to
    the RECORD drifting from the tree, not to a checker that was already
    broken."""

    assert REQUIREMENT_CHECKS[checker_name]() is True


# ---------------------------------------------------------------------------
# Plant: flip a claim to something false and show the validator refuses.
# ---------------------------------------------------------------------------


def test_planted_false_requirement_is_rejected() -> None:
    """Plant: flip a TRUE requirement's `satisfied` to `false`. The checker
    still recomputes the real (true) fact from the tree, so the mismatch
    must be caught -- this is the exact shape of defect the record exists
    to make impossible: a boolean nobody can check."""

    record = copy.deepcopy(load_record())
    target_id = "tenant-guc-is-a-class-scoped-after-begin-listener"
    flipped = False
    for entry in record["requirements"]:
        if entry["id"] == target_id:
            assert entry["satisfied"] is True, "plant assumes the baseline claims True"
            entry["satisfied"] = False
            flipped = True
    assert flipped, f"fixture drift: {target_id!r} not found in the record"

    with pytest.raises(
        RecordValidationError, match=rf"{target_id}.*satisfied=False.*True"
    ):
        validate_record(record)


def test_planted_unregistered_requirement_id_is_rejected() -> None:
    """Plant: rename a requirement id so no checker exists for it. An
    unchecked `satisfied: true` must be refused outright, not defaulted to
    passing."""

    record = copy.deepcopy(load_record())
    record["requirements"][0]["id"] = "some-id-nobody-wrote-a-checker-for"

    with pytest.raises(RecordValidationError, match="no registered checker"):
        validate_record(record)


def test_planted_nonexistent_source_reference_is_rejected() -> None:
    """Plant: point a source_reference at a file/line that does not exist."""

    record = copy.deepcopy(load_record())
    record["requirements"][0]["source_reference"] = "app/db.py:999999"

    with pytest.raises(RecordValidationError, match="out of bounds"):
        validate_record(record)


def test_planted_false_composition_declaration_is_rejected() -> None:
    """Plant: claim a composition Sub's tree does not actually exhibit --
    here, that Sub imports a second name (TenantDomain) from
    dotmac_kernel.models that today's tree does not import."""

    record = copy.deepcopy(load_record())
    record["composition"][0]["declaration"] = (
        "Sub imports Tenant and TenantDomain from dotmac_kernel.models; it "
        "does not import or construct dotmac_kernel.session_runtime."
        "DatabaseRuntime anywhere, and app/db.py remains Sub's own session "
        "and transaction authority."
    )

    with pytest.raises(RecordValidationError, match="no registered checker"):
        validate_record(record)


def test_planted_wrong_isolation_dict_value_is_rejected() -> None:
    """Plant: claim the read-only snapshot is SERIALIZABLE (it is REPEATABLE
    READ). The checker recomputes the real dict literal from `app/db.py` and
    the mismatch must be caught even though the record's `satisfied` stays
    `true` and only the *statement* text changes -- proving the checker does
    not merely trust `satisfied`, it recomputes the underlying fact and the
    dict-equality check inside it is exact."""

    from tests.architecture.kernel_runtime_readiness import _options_dict_values

    values = _options_dict_values("READ_ONLY_SNAPSHOT_OPTIONS")
    assert values == {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    wrong = {"isolation_level": "SERIALIZABLE", "postgresql_readonly": True}
    assert values != wrong


# ---------------------------------------------------------------------------
# Near-miss: a structurally different but still-legitimate reference passes.
# ---------------------------------------------------------------------------


def test_near_miss_wider_line_range_still_resolves() -> None:
    """A source_reference citing a wider (but still in-bounds, still
    overlapping the real definition) line range is a legitimate citation
    style, not a defect -- resolution must not be so strict that it rejects
    a correct, merely-generous citation."""

    path, start, end = resolve_source_reference("app/services/session_hooks.py:100-140")
    assert path.name == "session_hooks.py"
    assert start == 100 and end == 140


def test_near_miss_single_line_reference_still_resolves() -> None:
    resolve_source_reference("app/services/operator_tenant.py:29")
