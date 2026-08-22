"""The readiness runbook must keep describing the contract it documents.

A runbook is read once, under pressure, by somebody who was not there when it
was written. The failure mode is not that it is wrong on the day it lands — it
is that the contract moves and the prose does not, so the reader follows a
sequence that no longer matches the code.

These are coherence checks, not prose review. Each one asserts that a fact the
runbook depends on is still true, and fails with the fact that changed.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.migration_source import cohort, programme, snapshot, surfaces
from app.migration_source.cohort import CohortEntityType
from app.migration_source.digest import MismatchCategory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = PROJECT_ROOT / "docs" / "ISP_COHORT1_MIGRATION_READINESS.md"
EXPORT_CLI = PROJECT_ROOT / "scripts" / "migration" / "export_isp_cohort_snapshot.py"


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def test_the_runbook_names_every_cutover_control() -> None:
    body = _runbook()
    missing = sorted(
        control.control_id
        for control in programme.BINDING.controls
        if control.control_id not in body
    )
    assert not missing, (
        "the runbook does not mention these Governance controls, so a reader "
        "following it would skip a gate:\n  " + "\n  ".join(missing)
    )


def test_the_runbook_names_the_open_decisions_that_block_the_cohort() -> None:
    body = _runbook()
    missing = sorted(
        decision
        for decision in programme.BINDING.unresolved_decision_ids
        if decision not in body
    )
    assert not missing, (
        "the runbook must name what is actually blocking, or it reads as a "
        "sequence somebody could start:\n  " + "\n  ".join(missing)
    )


def test_the_runbook_pins_the_accepted_governance_revision() -> None:
    assert programme.ACCEPTED_REVISION in _runbook(), (
        "the runbook must cite the immutable revision its cohort definition "
        "comes from; a programme reference with no revision drifts silently"
    )


def test_the_runbook_covers_every_entity_type_in_its_backfill_order() -> None:
    body = _runbook()
    missing = sorted(
        entity_type.value
        for entity_type in CohortEntityType
        if entity_type.value not in body
    )
    assert not missing, (
        "these entity types have no place in the documented backfill order, "
        "so an import following the runbook would leave them out:\n  "
        + "\n  ".join(missing)
    )


def test_the_runbook_lists_every_mismatch_verdict_with_a_response() -> None:
    body = _runbook()
    missing = sorted(
        category.value for category in MismatchCategory if category.value not in body
    )
    assert not missing, (
        "a verdict with no documented response is a verdict somebody will "
        "improvise around:\n  " + "\n  ".join(missing)
    )


def test_the_runbook_states_the_current_writer_counts() -> None:
    body = _runbook()
    production = len(surfaces.production_writer_paths())
    assert str(production) in body, (
        f"the runbook must quote the real production writer count ({production}); "
        "a stale number understates or overstates what a cutover has to displace"
    )


def test_the_runbook_names_the_derived_fields_a_target_must_recompute() -> None:
    body = _runbook()
    missing = sorted(
        field
        for field in snapshot.CustomerAccountRecord.DERIVED_FIELDS
        if field not in body and field.rstrip("_") not in body
    )
    assert not missing, (
        "a derived field the runbook does not name is a field a destination "
        "will adopt as authoritative:\n  " + "\n  ".join(missing)
    )


def test_every_cli_flag_the_runbook_shows_actually_exists() -> None:
    """A documented invocation that does not parse is worse than none."""

    tree = ast.parse(EXPORT_CLI.read_text(encoding="utf-8"), filename=str(EXPORT_CLI))
    declared = {
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }
    body = _runbook()
    documented = {
        token.strip("\\`")
        for line in body.splitlines()
        if line.strip().startswith(("python scripts/", "--")) or "--" in line
        for token in line.split()
        if token.startswith("--")
    }
    unknown = sorted(flag for flag in documented if flag not in declared)
    assert not unknown, (
        "the runbook shows flags the export CLI does not accept:\n  "
        + "\n  ".join(unknown)
    )


def test_the_entity_table_matches_the_declared_cohort_surface() -> None:
    """The ownership map's twelve-row table must not drift from `cohort.py`.

    It drifted twice. The declared owner for `addresses` said "none declared"
    when `customer.accounts` owns it, and the target component still said
    `dotmac-customers` after dec-isp-007 moved it to `dotmac-addresses`.
    Neither was caught, because the only guard on this document checked the
    writer list.

    A wrong owner in a table people read before a cutover is worse than a
    missing one: it makes a bypass of a real owner look like unowned debt, and
    it nearly produced a duplicate address service.
    """

    document = (
        Path(__file__).resolve().parents[2] / "docs" / "ISP_COHORT1_SOURCE_OWNERSHIP.md"
    ).read_text(encoding="utf-8")

    rows = {
        line.split("|")[1].strip().strip("`"): line
        for line in document.splitlines()
        if line.startswith("| `") and line.count("|") >= 6
    }
    problems: list[str] = []
    for declared in cohort.COHORT_TABLES:
        row = rows.get(declared.entity_type.value)
        if row is None:
            problems.append(f"{declared.entity_type.value}: no row in the table")
            continue
        expected_owner = declared.owning_service or "none declared"
        if expected_owner not in row:
            problems.append(
                f"{declared.entity_type.value}: table does not name owner "
                f"{expected_owner!r}"
            )
        if declared.expected_target_component.value not in row:
            problems.append(
                f"{declared.entity_type.value}: table does not name target "
                f"{declared.expected_target_component.value!r}"
            )
    assert not problems, (
        "docs/ISP_COHORT1_SOURCE_OWNERSHIP.md disagrees with "
        "app/migration_source/cohort.py:\n  " + "\n  ".join(problems)
    )


def test_the_runbook_explains_the_absent_online_adapter() -> None:
    """An absence with no stated reason reads as an oversight."""

    body = _runbook()
    assert "Why there is no online export endpoint" in body
    assert "ctl-isp-002" in body


def test_no_online_surface_reaches_the_cohort_export() -> None:
    """The deferral is enforced, not merely documented.

    ADR 0012's 2026-08-21 amendment declines an HTTP export route: it would be
    a standing network-reachable egress surface for the whole cohort's customer
    identity data, and `ctl-isp-002` is open so nothing is positioned to call
    it. A decision recorded only in prose is a decision the next contributor
    reverses by accident, so this fails the build if any route module imports
    the export owner.

    When the conditions in the ADR amendment are met, this test is deleted in
    the same change that adds the route — deliberately, with the guards the
    amendment names.
    """

    online_roots = (
        PROJECT_ROOT / "app" / "api",
        PROJECT_ROOT / "app" / "web",
        PROJECT_ROOT / "app" / "websocket",
    )
    offenders: list[str] = []
    for root in online_roots:
        if not root.exists():  # pragma: no cover - layout guard
            continue
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                offenders.extend(
                    f"{path.relative_to(PROJECT_ROOT)} imports {name}"
                    for name in names
                    if name.startswith("app.services.migration_source_export")
                )
    assert not offenders, (
        "an online surface reaches the cohort export. See "
        "docs/adr/0012-isp-cohort-source-readiness.md, amendment 2026-08-21, "
        "for the conditions that have to hold first:\n  " + "\n  ".join(offenders)
    )


def test_the_readiness_claims_and_the_cohort_state_are_both_stated() -> None:
    """Both halves, together. Either alone is misleading."""

    body = _runbook()
    for claim in programme.CLAIMS:
        assert claim.value.replace("_", " ") in body.lower() or claim.value in body, (
            f"the runbook does not state the {claim.value} claim"
        )
    assert programme.BINDING.cohort_state.value in body, (
        "the runbook must say the cohort is still blocked next to what the "
        "source-readiness work achieved; the claims alone read as permission"
    )
