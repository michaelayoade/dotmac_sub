"""Enforce complete typed contracts for new and migrated SOT owners."""

from __future__ import annotations

from dataclasses import replace
from functools import cache
from pathlib import Path

from app.services import sot_relationships
from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    contract_validation_errors,
)
from scripts.architecture.sot_debt import read_name_baseline

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_BASELINE = Path(__file__).with_name("sot_manifest_legacy_baseline.txt")


@cache
def _legacy_services() -> set[str]:
    return {
        service.name
        for service in sot_relationships.all_services()
        if not service.is_contracted
    }


def test_legacy_manifest_baseline_is_sorted_and_unique() -> None:
    entries = [
        line.strip()
        for line in LEGACY_BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert entries == sorted(set(entries))


def test_no_new_uncontracted_manifest_services() -> None:
    current = _legacy_services()
    baseline = read_name_baseline(LEGACY_BASELINE)
    new = sorted(current - baseline)

    assert not new, (
        "new registry services have no typed ServiceContract. Complete the "
        "manifest declaration; do not expand the legacy baseline:\n  "
        + "\n  ".join(new)
    )


def test_legacy_manifest_baseline_only_shrinks() -> None:
    current = _legacy_services()
    baseline = read_name_baseline(LEGACY_BASELINE)
    resolved = sorted(baseline - current)

    assert not resolved, (
        "services are now contracted or removed; delete them from the "
        "shrink-only legacy manifest baseline:\n  " + "\n  ".join(resolved)
    )


def test_contracted_manifest_references_exist() -> None:
    missing: list[str] = []
    invalid: list[str] = []
    for service in sot_relationships.all_services():
        if service.contract is None:
            continue
        for reference in service.contract.design_refs + service.contract.test_refs:
            path = Path(reference)
            if path.is_absolute() or ".." in path.parts:
                invalid.append(f"{service.name}: {reference}")
            elif not (PROJECT_ROOT / path).is_file():
                missing.append(f"{service.name}: {reference}")

    assert not invalid, "invalid manifest references:\n  " + "\n  ".join(invalid)
    assert not missing, "missing manifest references:\n  " + "\n  ".join(missing)


def test_owner_lookup_requires_an_exact_concern() -> None:
    owner = sot_relationships.owning_service_for("primary NAS session resolution")

    assert owner is not None
    assert owner.name == "sessions.radius_resolution"
    assert sot_relationships.owning_service_for("primary NAS session") is None


def test_state_writer_contract_requires_transaction_errors_and_events() -> None:
    service = SOTService(
        name="example.writer",
        module="app.services.example",
        owns=("example authoritative state",),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="example authoritative state",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("example source",),
                    canonical_writer="example.writer",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="example source",
                    owner="example.source",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="example_records",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="incorrect read boundary",
                locking="none",
                idempotency="none",
                retries="none",
            ),
            errors=ErrorContract(domain_codes=(), mapping_owner="adapters"),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="example.writer",
            ),
            steward="example team",
            design_refs=("docs/example.md",),
            test_refs=("tests/test_example.py",),
        ),
    )

    errors = contract_validation_errors(
        service,
        service_names={"example.source", "example.writer", "events.dispatcher"},
    )

    assert any("transaction mode is 'read_only'" in error for error in errors)
    assert any("declares no domain error codes" in error for error in errors)
    assert any("has no event contract" in error for error in errors)

    assert service.contract is not None
    managed_service = replace(
        service,
        contract=replace(
            service.contract,
            transaction=replace(
                service.contract.transaction,
                mode=TransactionMode.OWNER_MANAGED,
            ),
        ),
    )
    managed_errors = contract_validation_errors(
        managed_service,
        service_names={"example.source", "example.writer", "events.dispatcher"},
    )
    assert any(
        "omits owner-command boundary error codes" in error for error in managed_errors
    )


def test_participant_writer_declares_nested_atomicity_without_public_executor_errors():
    service = SOTService(
        name="example.writer",
        module="app.services.example",
        owns=("example authoritative state",),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="example authoritative state",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("example source",),
                    canonical_writer="example.writer",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="example source",
                    owner="example.source",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="example_records",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary="Named coordinators own the surrounding transaction.",
                locking="The participant locks its authoritative row.",
                idempotency="Equivalent state is a no-op.",
                retries="The coordinator retries the complete command.",
            ),
            errors=ErrorContract(
                domain_codes=("example.writer.invalid_input",),
                mapping_owner="named coordinators",
            ),
            events=EventContract(
                event_types=("example.state_changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Version 1 is additive.",
                replay="Rebuild from example_records.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="example.writer",
            ),
            steward="example team",
            design_refs=("docs/example.md",),
            test_refs=("tests/test_example.py",),
        ),
    )

    assert (
        contract_validation_errors(
            service,
            service_names={"example.source", "example.writer", "events.dispatcher"},
        )
        == ()
    )
