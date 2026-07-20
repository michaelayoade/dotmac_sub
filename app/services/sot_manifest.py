"""Typed contracts for the canonical source-of-truth architecture manifest.

The relationship registry may retain uncontracted legacy entries only while
they remain in the shrink-only architecture baseline. New and migrated owners
use :class:`ServiceContract`; validation then makes their authority, inputs,
transaction, errors, events, projections, repair, and migration state
mechanically inspectable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection


class OwnerRole(StrEnum):
    """One non-overlapping responsibility within an ownership boundary."""

    AUTHORITATIVE_RECORD = "authoritative_record"
    OBSERVATION_COLLECTOR = "observation_collector"
    RESOLVER = "resolver"
    POLICY = "policy"
    COMMAND_WRITER = "command_writer"
    APPLICATION_COORDINATOR = "application_coordinator"
    EVENT_POLICY = "event_policy"
    RECONCILER = "reconciler"
    PROJECTION_WRITER = "projection_writer"
    TRANSPORT = "transport"


class AuthorityKind(StrEnum):
    """How a contracted owner may interpret an authoritative input."""

    AUTHORITATIVE_RECORD = "authoritative_record"
    OBSERVATION = "observation"
    DERIVED_PROJECTION = "derived_projection"
    CONTROL_INPUT = "control_input"
    EXTERNAL_OBSERVATION = "external_observation"


class TransactionMode(StrEnum):
    """Where database transaction responsibility lives for the service."""

    NOT_APPLICABLE = "not_applicable"
    READ_ONLY = "read_only"
    PARTICIPANT = "participant"
    OWNER_MANAGED = "owner_managed"
    COORDINATOR_MANAGED = "coordinator_managed"


class AuthorityMigrationState(StrEnum):
    """Observable state of an authority migration."""

    NATIVE = "native"
    INVENTORIED = "inventoried"
    SHADOWING = "shadowing"
    CUTOVER_READY = "cutover_ready"
    CUT_OVER = "cut_over"
    COMPLETE = "complete"


@dataclass(frozen=True)
class AuthorityInput:
    """One named fact source consumed by the contracted service."""

    name: str
    owner: str
    kind: AuthorityKind
    source: str


@dataclass(frozen=True)
class ConcernContract:
    """Role and input mapping for one exact string in ``SOTService.owns``."""

    name: str
    role: OwnerRole
    input_names: tuple[str, ...]
    canonical_writer: str | None = None


@dataclass(frozen=True)
class TransactionContract:
    """Atomicity, concurrency, idempotency, and retry boundary."""

    mode: TransactionMode
    boundary: str
    locking: str
    idempotency: str
    retries: str


@dataclass(frozen=True)
class ErrorContract:
    """Stable domain errors and adapter mapping responsibility."""

    domain_codes: tuple[str, ...]
    mapping_owner: str
    retryable_codes: tuple[str, ...] = ()
    fail_closed_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventContract:
    """Versioned event envelope and delivery/replay policy."""

    event_types: tuple[str, ...]
    schema_version: int
    delivery_owner: str
    compatibility: str
    replay: str


@dataclass(frozen=True)
class ProjectionContract:
    """Freshness, drift detection, deterministic rebuild, and repair owner."""

    name: str
    input_names: tuple[str, ...]
    writer: str
    freshness: str
    stale_behavior: str
    drift_signal: str
    rebuild_operation: str
    repair_owner: str


@dataclass(frozen=True)
class MigrationContract:
    """Authority migration and fallback-retirement evidence."""

    state: AuthorityMigrationState
    new_owner: str
    old_owner: str | None = None
    verification: str | None = None
    cutover_gate: str | None = None
    fallback_retirement: str | None = None


@dataclass(frozen=True)
class ServiceContract:
    """Complete machine-readable contract for one registered service."""

    concerns: tuple[ConcernContract, ...]
    authoritative_inputs: tuple[AuthorityInput, ...]
    transaction: TransactionContract
    errors: ErrorContract
    migration: MigrationContract
    steward: str
    design_refs: tuple[str, ...]
    test_refs: tuple[str, ...]
    events: EventContract | None = None
    projections: tuple[ProjectionContract, ...] = ()
    manifest_version: int = 1


@dataclass(frozen=True)
class SOTService:
    """A named owner and its optional fully contracted manifest entry."""

    name: str
    module: str
    owns: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    notes: str | None = None
    contract: ServiceContract | None = None

    @property
    def is_contracted(self) -> bool:
        return self.contract is not None


_WRITER_ROLES = {
    OwnerRole.AUTHORITATIVE_RECORD,
    OwnerRole.OBSERVATION_COLLECTOR,
    OwnerRole.COMMAND_WRITER,
    OwnerRole.RECONCILER,
    OwnerRole.PROJECTION_WRITER,
}
_TRANSACTIONAL_WRITER_ROLES = _WRITER_ROLES | {
    OwnerRole.APPLICATION_COORDINATOR,
}
_OWNER_COMMAND_ERROR_SUFFIXES = (
    "active_caller_transaction",
    "command_contract_violation",
    "invalid_command_context",
    "nested_owner_command",
    "nested_transaction_completion",
)


def owner_command_boundary_error_codes(service_name: str) -> tuple[str, ...]:
    """Stable runtime errors every transactional command owner declares."""

    return tuple(f"{service_name}.{suffix}" for suffix in _OWNER_COMMAND_ERROR_SUFFIXES)


def _duplicates(values: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _require_text(
    errors: list[str], service_name: str, label: str, value: str | None
) -> None:
    if value is None or not value.strip():
        errors.append(f"service {service_name!r} contract has empty {label}")


def contract_validation_errors(
    service: SOTService,
    *,
    service_names: Collection[str],
) -> tuple[str, ...]:
    """Return every structural error in a contracted service declaration."""

    contract = service.contract
    if contract is None:
        return ()

    errors: list[str] = []
    if contract.manifest_version != 1:
        errors.append(
            f"service {service.name!r} uses unsupported manifest version "
            f"{contract.manifest_version}"
        )

    concern_names = tuple(concern.name for concern in contract.concerns)
    missing_concerns = sorted(set(service.owns) - set(concern_names))
    extra_concerns = sorted(set(concern_names) - set(service.owns))
    if missing_concerns:
        errors.append(
            f"service {service.name!r} has uncontracted concerns: "
            + ", ".join(missing_concerns)
        )
    if extra_concerns:
        errors.append(
            f"service {service.name!r} contract has undeclared concerns: "
            + ", ".join(extra_concerns)
        )
    for duplicate_concern in sorted(_duplicates(concern_names)):
        errors.append(
            f"service {service.name!r} repeats concern contract {duplicate_concern!r}"
        )

    input_names = tuple(item.name for item in contract.authoritative_inputs)
    for input_name in sorted(_duplicates(input_names)):
        errors.append(
            f"service {service.name!r} repeats authoritative input {input_name!r}"
        )
    for item in contract.authoritative_inputs:
        _require_text(errors, service.name, "authoritative input name", item.name)
        _require_text(errors, service.name, f"input {item.name!r} source", item.source)
        if item.kind is AuthorityKind.EXTERNAL_OBSERVATION:
            if not item.owner.startswith("external:"):
                errors.append(
                    f"service {service.name!r} external input {item.name!r} must "
                    "use an external:<system> owner"
                )
        elif item.owner not in service_names:
            errors.append(
                f"service {service.name!r} input {item.name!r} has unknown owner "
                f"{item.owner!r}"
            )

    roles = {concern.role for concern in contract.concerns}
    for concern in contract.concerns:
        if not concern.input_names:
            errors.append(
                f"service {service.name!r} concern {concern.name!r} has no inputs"
            )
        for input_name in concern.input_names:
            if input_name not in input_names:
                errors.append(
                    f"service {service.name!r} concern {concern.name!r} references "
                    f"unknown input {input_name!r}"
                )
        if concern.role in _WRITER_ROLES:
            if concern.canonical_writer != service.name:
                errors.append(
                    f"service {service.name!r} writer concern {concern.name!r} must "
                    "name itself as canonical_writer"
                )
        elif concern.canonical_writer is not None:
            errors.append(
                f"service {service.name!r} non-writer concern {concern.name!r} "
                "cannot declare a canonical_writer"
            )

    transaction = contract.transaction
    for transaction_label, transaction_value in (
        ("transaction boundary", transaction.boundary),
        ("locking contract", transaction.locking),
        ("idempotency contract", transaction.idempotency),
        ("retry contract", transaction.retries),
    ):
        _require_text(
            errors,
            service.name,
            transaction_label,
            transaction_value,
        )
    if roles & _TRANSACTIONAL_WRITER_ROLES:
        if transaction.mode not in {
            TransactionMode.PARTICIPANT,
            TransactionMode.OWNER_MANAGED,
            TransactionMode.COORDINATOR_MANAGED,
        }:
            errors.append(
                f"service {service.name!r} has a writer/coordinator role but "
                f"transaction mode is {transaction.mode.value!r}"
            )
        if (
            OwnerRole.APPLICATION_COORDINATOR in roles
            and transaction.mode is TransactionMode.PARTICIPANT
        ):
            errors.append(
                f"service {service.name!r} application coordinator cannot use "
                "participant transaction mode"
            )
    elif transaction.mode in {
        TransactionMode.PARTICIPANT,
        TransactionMode.OWNER_MANAGED,
        TransactionMode.COORDINATOR_MANAGED,
    }:
        errors.append(
            f"service {service.name!r} has no writer/coordinator concern for "
            f"transaction mode {transaction.mode.value!r}"
        )

    error_contract = contract.errors
    _require_text(
        errors, service.name, "error mapping owner", error_contract.mapping_owner
    )
    unknown_retryable = sorted(
        set(error_contract.retryable_codes) - set(error_contract.domain_codes)
    )
    if unknown_retryable:
        errors.append(
            f"service {service.name!r} has retryable codes absent from domain "
            f"codes: {', '.join(unknown_retryable)}"
        )
    if roles & _WRITER_ROLES and not error_contract.domain_codes:
        errors.append(
            f"service {service.name!r} writes state but declares no domain error codes"
        )
    if transaction.mode in {
        TransactionMode.OWNER_MANAGED,
        TransactionMode.COORDINATOR_MANAGED,
    }:
        missing_boundary_errors = sorted(
            set(owner_command_boundary_error_codes(service.name))
            - set(error_contract.domain_codes)
        )
        if missing_boundary_errors:
            errors.append(
                f"service {service.name!r} omits owner-command boundary error "
                f"codes: {', '.join(missing_boundary_errors)}"
            )

    if roles & _WRITER_ROLES:
        if contract.events is None:
            errors.append(
                f"service {service.name!r} writes state but has no event contract"
            )
    if contract.events is not None:
        event_contract = contract.events
        if not event_contract.event_types:
            errors.append(f"service {service.name!r} event contract has no event types")
        if event_contract.schema_version < 1:
            errors.append(
                f"service {service.name!r} event schema version must be positive"
            )
        if event_contract.delivery_owner not in service_names:
            errors.append(
                f"service {service.name!r} event contract has unknown delivery owner "
                f"{event_contract.delivery_owner!r}"
            )
        _require_text(
            errors, service.name, "event compatibility", event_contract.compatibility
        )
        _require_text(errors, service.name, "event replay", event_contract.replay)

    projection_names = tuple(item.name for item in contract.projections)
    for projection_name in sorted(_duplicates(projection_names)):
        errors.append(
            f"service {service.name!r} repeats projection {projection_name!r}"
        )
    if roles & {OwnerRole.PROJECTION_WRITER, OwnerRole.RECONCILER}:
        if not contract.projections:
            errors.append(
                f"service {service.name!r} owns projection/repair but has no "
                "projection contract"
            )
    for projection in contract.projections:
        for input_name in projection.input_names:
            if input_name not in input_names:
                errors.append(
                    f"service {service.name!r} projection {projection.name!r} "
                    f"references unknown input {input_name!r}"
                )
        for label, owner in (
            ("writer", projection.writer),
            ("repair owner", projection.repair_owner),
        ):
            if owner not in service_names:
                errors.append(
                    f"service {service.name!r} projection {projection.name!r} has "
                    f"unknown {label} {owner!r}"
                )
        for projection_label, projection_value in (
            ("freshness", projection.freshness),
            ("stale behavior", projection.stale_behavior),
            ("drift signal", projection.drift_signal),
            ("rebuild operation", projection.rebuild_operation),
        ):
            _require_text(
                errors,
                service.name,
                f"projection {projection.name!r} {projection_label}",
                projection_value,
            )

    migration = contract.migration
    if migration.new_owner != service.name:
        errors.append(
            f"service {service.name!r} migration new_owner must name the service"
        )
    if migration.state is AuthorityMigrationState.NATIVE:
        if migration.old_owner is not None:
            errors.append(
                f"service {service.name!r} native authority cannot have old_owner"
            )
    else:
        for migration_label, migration_value in (
            ("old owner", migration.old_owner),
            ("verification", migration.verification),
            ("cutover gate", migration.cutover_gate),
            ("fallback retirement", migration.fallback_retirement),
        ):
            _require_text(
                errors,
                service.name,
                f"migration {migration_label}",
                migration_value,
            )

    _require_text(errors, service.name, "steward", contract.steward)
    if not contract.design_refs:
        errors.append(f"service {service.name!r} contract has no design references")
    if not contract.test_refs:
        errors.append(f"service {service.name!r} contract has no test references")

    return tuple(sorted(errors))
