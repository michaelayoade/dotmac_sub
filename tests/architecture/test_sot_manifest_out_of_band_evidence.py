"""``TransactionMode.OUT_OF_BAND_EVIDENCE`` is a narrow, declared exception.

It exists for evidence of an external, irreversible effect that its owner writes
on an independent unit of work so the record survives the caller's rollback
(first use: ADR 0017, ``access.enforcement_evidence``). The contract validator
must keep it narrow: observation collectors only, an approving ADR cited, and no
event contract (the write emits none). Within that shape it is exempt from the
"writers declare domain error codes and an event contract" rules, because the
writer never raises into its caller and emits nothing.
"""

from __future__ import annotations

from dataclasses import replace

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

_NAMES = {"example.evidence", "example.effect_owner", "events.dispatcher"}


def _evidence_service(**contract_overrides) -> SOTService:
    contract = ServiceContract(
        concerns=(
            ConcernContract(
                name="example effect evidence",
                role=OwnerRole.OBSERVATION_COLLECTOR,
                input_names=("example effect outcome",),
                canonical_writer="example.evidence",
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="example effect outcome",
                owner="example.effect_owner",
                kind=AuthorityKind.OBSERVATION,
                source="typed outcome from the effect owner",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.OUT_OF_BAND_EVIDENCE,
            boundary="own create_session() unit of work per record",
            locking="no foreign keys; short lock_timeout",
            idempotency="upsert on the natural key",
            retries="none; failure is logged and dropped",
        ),
        errors=ErrorContract(domain_codes=(), mapping_owner="example.evidence"),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner="example.evidence",
        ),
        steward="example team",
        design_refs=("docs/adr/0017-enforcement-application-evidence.md",),
        test_refs=("tests/test_example.py",),
    )
    return SOTService(
        name="example.evidence",
        module="app.services.example_evidence",
        owns=("example effect evidence",),
        contract=replace(contract, **contract_overrides),
    )


def test_a_well_formed_out_of_band_evidence_contract_is_valid() -> None:
    """No domain error codes and no event contract are required in this mode."""
    assert contract_validation_errors(_evidence_service(), service_names=_NAMES) == ()


def test_the_same_contract_in_owner_managed_mode_would_be_rejected() -> None:
    """Sensitivity: the exemptions above belong to the mode, not to the shape.
    The identical contract under owner_managed fails the writer rules."""
    service = _evidence_service()
    assert service.contract is not None
    managed = replace(
        service,
        contract=replace(
            service.contract,
            transaction=replace(
                service.contract.transaction, mode=TransactionMode.OWNER_MANAGED
            ),
        ),
    )
    errors = contract_validation_errors(managed, service_names=_NAMES)
    assert any("declares no domain error codes" in error for error in errors)
    assert any("has no event contract" in error for error in errors)


def test_out_of_band_evidence_refuses_a_non_observation_writer() -> None:
    service = _evidence_service()
    assert service.contract is not None
    command = replace(
        service,
        contract=replace(
            service.contract,
            concerns=(
                replace(service.contract.concerns[0], role=OwnerRole.COMMAND_WRITER),
            ),
        ),
    )
    errors = contract_validation_errors(command, service_names=_NAMES)
    assert any("allows only observation collectors" in error for error in errors)


def test_out_of_band_evidence_must_cite_an_adr() -> None:
    errors = contract_validation_errors(
        _evidence_service(design_refs=("docs/SOT_RELATIONSHIP_MAP.md",)),
        service_names=_NAMES,
    )
    assert any("must cite the approving ADR" in error for error in errors)


def test_out_of_band_evidence_refuses_an_event_contract() -> None:
    errors = contract_validation_errors(
        _evidence_service(
            events=EventContract(
                event_types=("example_recorded",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="additive",
                replay="idempotent",
            )
        ),
        service_names=_NAMES,
    )
    assert any("emits no domain events" in error for error in errors)
