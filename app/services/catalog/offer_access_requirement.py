"""``service_intent.offer_access_requirement`` — the sole owner of:

- access-classified offer-version admission
- the immutable access requirement for an exact offer version
- reviewed classification of legacy/unclassified versions

Release 1 only. Release 2 (rejecting ``unclassified`` at admission and
dropping the DB default) is separate, later work — see
``docs/designs/CATALOG_ACCESS_REQUIREMENT_AUTHORITY.md``.

``service_intent.catalog_policy`` (``app/services/catalog/policies.py``) is a
deliberately separate, untouched owner. This module never imports it and
never reads/writes its tables.

Both public commands (admission and reviewed classification) enter through
``execute_owner_command`` and perform their own persistence and transaction
completion here — a caller builds the command and reads the result; it never
constructs the ``OfferVersion`` row or the classification row itself.

Authorization for the two commands lives at different layers, deliberately:
admission is gated entirely at the ROUTE (``app/api/catalog.py``'s own
router-level ``catalog:write`` gate, combined with the route's
``require_any_permission(catalog:billing_write, catalog:offer_version:
admission)`` dependency — the ACTUAL effective requirement is the compound
``catalog:write AND (catalog:billing_write OR catalog:offer_version:
admission)``, never a pure OR/standalone-narrower-permission alternative to
``catalog:write`` itself) — this module makes NO authorization decision for
admission, and ``AdmitOfferVersionCommand.principal`` is audit/attribution
evidence only. Classification has no such pre-authorizing route (its only
caller is a trust-the-operator CLI); its permission is re-verified fresh,
inside this module, immediately before the write.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import (
    AccessRequirement,
    BillingCycle,
    CatalogOffer,
    ContractTerm,
    OfferAccessRequirementClassification,
    OfferStatus,
    OfferVersion,
)
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.models.system_user import SystemUser
from app.schemas.catalog import OfferVersionCreate
from app.services import catalog_billing_governance as billing_governance
from app.services import settings_spec
from app.services.audit_adapter import stage_audit_event
from app.services.auth_dependencies import has_permission
from app.services.common import validate_enum
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.system_user_assignments import system_user_role_names

OWNER = "service_intent.offer_access_requirement"
CLASSIFY_PERMISSION = "catalog:offer_access_requirement:classify"
ADMISSION_SCOPE = "catalog:offer_version:admission"

_ADMIT_CONCERN = "access-classified offer-version admission"
_ADMIT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_ADMIT_CONCERN,
    name="admit_offer_version",
)

_CLASSIFY_CONCERN = "reviewed classification of legacy/unclassified versions"
_CLASSIFY_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=_CLASSIFY_CONCERN,
    name="classify_offer_version_access_requirement",
)

#: Real classifications. ``unclassified`` is never a valid classification
#: target — it is the state being replaced, never the state applied.
_REAL_CLASSIFICATIONS = (
    AccessRequirement.network_access,
    AccessRequirement.no_network_access,
)

#: Match ``offer_access_requirement_classifications.idempotency_key``/
#: ``.review_reference`` (``app/models/catalog.py``'s ``String(120)``/
#: ``String(200)`` columns) exactly. Checked here, before either value
#: reaches the database, so an oversized CLI/API input is a typed
#: validation error instead of a raw database error.
_IDEMPOTENCY_KEY_MAX_LENGTH = 120
_REVIEW_REFERENCE_MAX_LENGTH = 200

#: Scope for admission's row in the shared ``idempotency_keys`` ledger
#: (``app/models/idempotency.py``) — the same generic replay-safe mechanism
#: several other owners already use, rather than a bespoke per-owner table.
_ADMISSION_IDEMPOTENCY_SCOPE = "offer_version_admission"

#: Name of the DB-level unique constraint on
#: ``(offer_versions.offer_id, offer_versions.version_number)``
#: (``alembic/versions/610_offer_versions_unique_version_number.py``). Used
#: to distinguish an actual duplicate-version-number race from an unrelated
#: integrity violation (e.g. a dangling FK on ``region_zone_id``) hitting the
#: same broad ``except IntegrityError`` — the latter must never be mislabeled
#: as ``duplicate_version_number``.
_DUPLICATE_VERSION_NUMBER_CONSTRAINT = "uq_offer_versions_offer_id_version_number"


class OfferAccessRequirementError(DomainError):
    """Fail-closed offer-access-requirement admission/classification error."""


def _error(
    suffix: str, message: str, *, retryable: bool, **details: object
) -> OfferAccessRequirementError:
    """Every call site states ``retryable`` explicitly (no default): the SOT
    manifest declares only ``stale_preview`` as retryable and every other
    refusal as terminal — a silent ``DomainError``-inherited default of
    ``True`` would contradict that contract for every other code."""

    return OfferAccessRequirementError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=dict(details),
        retryable=retryable,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _admission_fingerprint(payload: OfferVersionCreate) -> str:
    """Fingerprint of the exact client-supplied admission request.

    Computed over the RAW payload the caller supplied (never over
    server-resolved catalog defaults) so a genuine retry of the same request
    fingerprints identically regardless of how default resolution evolves.

    ``model_dump()`` alone is not enough: Pydantic fills every OMITTED
    optional field (e.g. ``billing_cycle``) with its schema default before
    this ever sees it, so an omitted field and an explicit value that
    happens to equal that same default are indistinguishable in the dumped
    dict — a request that left ``billing_cycle`` unset (letting
    ``_admit``'s own settings-resolved default decide) could silently
    collide or diverge against one that explicitly asked for the schema
    default. Including ``model_fields_set`` makes "omitted" and "explicitly
    set to the default value" fingerprint differently.
    """

    return _digest(
        {
            "payload": payload.model_dump(mode="json"),
            "fields_set": sorted(payload.model_fields_set),
        }
    )


def _is_duplicate_version_number_violation(exc: IntegrityError) -> bool:
    """True only for the specific (offer_id, version_number) unique violation.

    Distinguishes it from any other ``IntegrityError`` (e.g. a dangling FK on
    ``region_zone_id``/``usage_allowance_id``/``sla_profile_id``/
    ``policy_set_id``) that must never be mislabeled as a duplicate. Matches
    on the real Postgres constraint name when available, and falls back to
    both column names appearing together (SQLite's error text names columns,
    not the constraint) — a plain FK-violation message names neither pair.
    """

    message = str(exc.orig) if exc.orig is not None else str(exc)
    if _DUPLICATE_VERSION_NUMBER_CONSTRAINT in message:
        return True
    return "offer_id" in message and "version_number" in message


def principal_label(system_user_id: UUID) -> str:
    """The recorded-identity string for an authenticated ``SystemUser``.

    This — never a caller-supplied free-text actor string — is what gets
    written as ``classified_by``, the audit actor, the event actor, and the
    ``authenticated_principal`` evidence field. It is derived here, once,
    from the exact id the caller authenticated, so nothing downstream can be
    told to attribute a classification to a different name than the one RBAC
    actually verified.
    """

    return f"system_user:{system_user_id}"


def _verify_classify_permission(db: Session, system_user_id: UUID) -> None:
    """Re-verify RBAC permission INSIDE the command's own transaction.

    A permission check performed earlier (e.g. for a CLI preview) is a
    look-then-act race: the grant could be revoked between that check and
    this command's commit. The only check that counts is the one taken here,
    against the live row, under the same lock/transaction as the write it
    gates.
    """

    # populate_existing=True forces a fresh read of this row even if an
    # earlier call in this same transaction already populated the identity
    # map for this id — without it, a second call here could silently return
    # the FIRST call's cached object and never observe a commit (e.g. a
    # revoked grant or a deactivated principal) that happened in between.
    user = db.get(SystemUser, system_user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise _error(
            "permission_denied",
            "Classification requires an active, authenticated staff principal.",
            retryable=False,
        )
    granted = has_permission(
        {
            "principal_id": str(system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, system_user_id)),
        },
        db,
        CLASSIFY_PERMISSION,
    )
    if not granted:
        raise _error(
            "permission_denied",
            "Classification requires the catalog:offer_access_requirement:"
            "classify permission.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class StaffPrincipal:
    """An admission attributed to an authenticated staff (system_user)
    principal. Evidence only — the ROUTE (``app/api/catalog.py``) already
    authorized the request via ``require_any_permission(catalog:billing_write,
    catalog:offer_version:admission)`` before this command ever runs; this
    id is recorded for audit/attribution and is never re-checked against RBAC
    here."""

    system_user_id: UUID


@dataclass(frozen=True, slots=True)
class ApiKeyPrincipal:
    """An admission attributed to an authenticated API-key principal.
    Evidence only, for the same reason as :class:`StaffPrincipal`."""

    api_key_id: UUID


@dataclass(frozen=True, slots=True)
class SystemAdmission:
    """An admission with no authenticated end-user context at all — internal
    tooling, seed data, or a test fixture calling this command directly
    rather than through the authorized HTTP route.

    Construction of this type is confined to an explicit, test-enforced
    allowlist of call sites
    (``tests/architecture/test_offer_access_requirement_boundary.py``). That
    guard is a BUILD-TIME/reviewed-call-site guarantee — it proves no
    *committed, non-test* file outside the allowlist constructs this type —
    it is NOT an unforgeable runtime credential: any code actually running
    inside that allowed module can still construct one. It exists to make an
    unreviewed new "no actor" admission path visible in review, not to
    cryptographically bind identity.
    """

    reason: str


#: The closed set of ways an admission can be attributed. Never used for an
#: authorization decision — see ``AdmitOfferVersionCommand.principal``'s
#: docstring.
AdmissionPrincipal = StaffPrincipal | ApiKeyPrincipal | SystemAdmission
_ADMISSION_PRINCIPAL_TYPES = (StaffPrincipal, ApiKeyPrincipal, SystemAdmission)


def admission_actor_label(principal: AdmissionPrincipal) -> str:
    """Human-readable ``CommandContext.actor`` label for one principal."""

    if isinstance(principal, StaffPrincipal):
        return f"system_user:{principal.system_user_id}"
    if isinstance(principal, ApiKeyPrincipal):
        return f"api_key:{principal.api_key_id}"
    return f"system:{principal.reason}"


def _admission_actor_evidence(
    principal: AdmissionPrincipal,
) -> tuple[str | None, str | None]:
    """``(actor_id, actor_type)`` evidence strings for the billing-governance
    audit participant — attribution only, never an authorization input."""

    if isinstance(principal, StaffPrincipal):
        return str(principal.system_user_id), "system_user"
    if isinstance(principal, ApiKeyPrincipal):
        return str(principal.api_key_id), "api_key"
    return None, None


def _lock_key(*parts: object) -> int:
    """Stable signed-bigint advisory-lock key for one tuple of parts.

    sha256-derived (never the builtin ``hash``, which is per-process salted)
    so every process/worker derives the same key for the same parts —
    mirrors ``app/services/radio_registration.py``'s ``mac_lock_key``.
    """

    digest = hashlib.sha256(
        "|".join(str(part) for part in parts).encode("utf-8")
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def _acquire_xact_lock(db: Session, *parts: object) -> None:
    """Transaction-scoped advisory lock, released at commit/rollback.

    No-op on non-PostgreSQL engines (SQLite tests), mirroring
    ``app/services/radio_registration.py::acquire_mac_lock`` and
    ``app/services/crm_subscriber_provisioning.py::_serialize_key``.
    """

    bind = db.get_bind()
    dialect_name = bind.dialect.name if bind is not None else ""
    if dialect_name != "postgresql":
        return
    db.execute(select(func.pg_advisory_xact_lock(_lock_key(*parts))))


# --------------------------------------------------------------------------
# Admission command (the only way OfferVersions.create persists a row) and
# the immutability guard (called from OfferVersions.update).
# --------------------------------------------------------------------------


def validate_admission_access_requirement(value: object) -> AccessRequirement:
    """Validate the explicit access requirement supplied at creation.

    Release 1 requires the field explicitly on every new
    :class:`OfferVersion` and accepts ``unclassified`` as an explicit value.
    There is no application-level fallback: a caller that did not supply a
    real enum member is rejected here rather than silently defaulted.
    """

    if not isinstance(value, AccessRequirement):
        raise _error(
            "invalid_access_requirement",
            "Offer version creation requires an explicit access_requirement value.",
            value=value,
            retryable=False,
        )
    return value


def assert_access_requirement_immutable(update_payload: Mapping[str, object]) -> None:
    """Fail closed if any offer-version update path ever carries this field.

    Defense in depth: ``OfferVersionUpdate`` deliberately has no
    ``access_requirement`` field, so this should be unreachable in practice.
    A future edit that reintroduces the field on the update schema fails
    loudly here instead of silently becoming a second, ungoverned writer.
    """

    if "access_requirement" in update_payload:
        raise _error(
            "immutable_access_requirement",
            "offer_versions.access_requirement is immutable outside the "
            "reviewed classification command.",
            retryable=False,
        )


@dataclass(frozen=True, slots=True)
class AdmitOfferVersionCommand:
    context: CommandContext
    payload: OfferVersionCreate
    #: REQUIRED, no default. Authorization for admission is decided entirely
    #: at the route layer (``app/api/catalog.py``'s router-level
    #: ``catalog:write`` gate together with the route's
    #: ``require_any_permission(catalog:billing_write,
    #: catalog:offer_version:admission)`` dependency) before this command is
    #: ever constructed — this command makes NO authorization decision of
    #: its own. ``principal`` is recorded for audit/attribution only; every
    #: caller must construct one explicitly (see ``AdmissionPrincipal``).
    principal: AdmissionPrincipal

    def __post_init__(self) -> None:
        # The closed union (``StaffPrincipal | ApiKeyPrincipal |
        # SystemAdmission``) is only a static type hint — Python does not
        # enforce it at runtime, so a caller passing ``principal=None`` or
        # any arbitrary object would otherwise be accepted silently. This
        # makes the closed set a real runtime guarantee: only omitting the
        # argument raises (a bare ``TypeError`` from the dataclass
        # constructor); passing something outside the union now raises here.
        if not isinstance(self.principal, _ADMISSION_PRINCIPAL_TYPES):
            raise TypeError(
                "AdmitOfferVersionCommand.principal must be a StaffPrincipal, "
                "ApiKeyPrincipal, or SystemAdmission instance; got "
                f"{type(self.principal).__name__!r}"
            )


@dataclass(frozen=True, slots=True)
class AdmitOfferVersionResult:
    """Typed outcome of ``admit_offer_version`` — matches this module's own
    ``OfferAccessRequirementClassificationResult`` pattern for the sibling
    command, rather than returning the mutable ORM row directly."""

    offer_version: OfferVersion
    #: True when this result reflects a prior admission returned by an
    #: exact idempotency-key replay, not a freshly persisted row.
    replayed: bool


def admit_offer_version(
    db: Session, command: AdmitOfferVersionCommand
) -> AdmitOfferVersionResult:
    """The one path that persists a new ``OfferVersion`` row.

    Owns its own transaction end to end: offer lookup, catalog-default
    resolution, access-requirement admission, the INSERT itself, and the
    billing-governance audit participant all run inside one
    ``execute_owner_command`` boundary. ``OfferVersions.create`` is a thin
    adapter over this — it does not construct the row itself.
    """

    return execute_owner_command(
        db,
        definition=_ADMIT_COMMAND,
        context=command.context,
        operation=lambda: _admit(db, command),
    )


def _admit(db: Session, command: AdmitOfferVersionCommand) -> AdmitOfferVersionResult:
    payload = command.payload
    offer = db.get(CatalogOffer, payload.offer_id)
    if not offer:
        raise _error(
            "offer_not_found",
            "Offer not found.",
            offer_id=str(payload.offer_id),
            retryable=False,
        )

    key = (command.context.idempotency_key or "").strip()
    if key and len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise _error(
            "idempotency_key_too_long",
            "idempotency_key exceeds the stored column's maximum length.",
            max_length=_IDEMPOTENCY_KEY_MAX_LENGTH,
            retryable=False,
        )

    # Lock order is fixed and identical for every admission: the idempotency
    # key first (if supplied), then the (offer_id, version_number) target.
    # Every caller acquires them in this same order, so this can never
    # deadlock against another admission.
    if key:
        _acquire_xact_lock(db, "offer_access_requirement:admit_idempotency", key)

    # Serialize concurrent admissions targeting the SAME (offer_id,
    # version_number) before either one can observe the other's uncommitted
    # existence check — without this lock, two racing retries of one
    # command (e.g. a client's retried POST after a dropped response) could
    # both pass the lookup below and both insert, creating a duplicate
    # version rather than a safe no-op/typed conflict.
    _acquire_xact_lock(
        db, "offer_access_requirement:admit", payload.offer_id, payload.version_number
    )

    fingerprint = _admission_fingerprint(payload)
    if key:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _ADMISSION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is not None:
            if reservation.ref_id != fingerprint:
                raise _error(
                    "idempotency_conflict",
                    "This idempotency key was already used for a different "
                    "offer version admission.",
                    offer_id=str(payload.offer_id),
                    version_number=payload.version_number,
                    retryable=False,
                )
            replayed_version = db.get(OfferVersion, reservation.account_id)
            if replayed_version is None:
                raise _error(
                    "idempotency_conflict",
                    "The prior admission result is no longer available.",
                    offer_id=str(payload.offer_id),
                    version_number=payload.version_number,
                    retryable=False,
                )
            return AdmitOfferVersionResult(
                offer_version=replayed_version, replayed=True
            )

    existing = db.scalar(
        select(OfferVersion).where(
            OfferVersion.offer_id == payload.offer_id,
            OfferVersion.version_number == payload.version_number,
        )
    )
    if existing is not None:
        raise _error(
            "duplicate_version_number",
            "This offer already has a version with this version_number; a "
            "retried admission must not resubmit an existing version_number "
            "as a new row.",
            offer_id=str(payload.offer_id),
            version_number=payload.version_number,
            retryable=False,
        )

    data = payload.model_dump()
    data["access_requirement"] = validate_admission_access_requirement(
        data.get("access_requirement")
    )
    fields_set = payload.model_fields_set
    if "billing_cycle" not in fields_set:
        default_billing_cycle = settings_spec.resolve_value(
            db, SettingDomain.catalog, "default_billing_cycle"
        )
        if default_billing_cycle:
            data["billing_cycle"] = validate_enum(
                default_billing_cycle, BillingCycle, "billing_cycle"
            )
    if "contract_term" not in fields_set:
        default_contract_term = settings_spec.resolve_value(
            db, SettingDomain.catalog, "default_contract_term"
        )
        if default_contract_term:
            data["contract_term"] = validate_enum(
                default_contract_term, ContractTerm, "contract_term"
            )
    if "status" not in fields_set:
        default_status = settings_spec.resolve_value(
            db, SettingDomain.catalog, "default_offer_status"
        )
        if default_status:
            data["status"] = validate_enum(default_status, OfferStatus, "status")

    version = OfferVersion(**data)
    db.add(version)
    try:
        db.flush()
    except IntegrityError as exc:
        # Defense in depth for a race the advisory lock above should already
        # have serialized (e.g. a writer that bypasses this command's lock):
        # a raw constraint violation still surfaces as this command's own
        # typed conflict. Only the SPECIFIC (offer_id, version_number)
        # unique violation is ever mislabeled as duplicate_version_number —
        # any other integrity violation (e.g. a dangling FK on
        # region_zone_id/usage_allowance_id/sla_profile_id/policy_set_id)
        # surfaces as its own distinct, honestly-named typed error.
        if _is_duplicate_version_number_violation(exc):
            raise _error(
                "duplicate_version_number",
                "This offer already has a version with this version_number.",
                offer_id=str(payload.offer_id),
                version_number=payload.version_number,
                retryable=False,
            ) from exc
        raise _error(
            "admission_integrity_violation",
            "Offer version admission violated a database integrity "
            "constraint unrelated to version_number uniqueness.",
            offer_id=str(payload.offer_id),
            version_number=payload.version_number,
            detail=str(exc.orig) if exc.orig is not None else str(exc),
            retryable=False,
        ) from exc

    if key:
        db.add(
            IdempotencyKey(
                scope=_ADMISSION_IDEMPOTENCY_SCOPE,
                key=key,
                account_id=version.id,
                ref_id=fingerprint,
            )
        )
        try:
            db.flush()
        except IntegrityError as exc:
            # The idempotency-key advisory lock above should already have
            # serialized concurrent uses of this exact key; this is defense
            # in depth against a writer that bypasses that lock.
            raise _error(
                "idempotency_conflict",
                "This idempotency key was already used for a different "
                "offer version admission.",
                offer_id=str(payload.offer_id),
                version_number=payload.version_number,
                retryable=False,
            ) from exc

    evidence_actor_id, evidence_actor_type = _admission_actor_evidence(
        command.principal
    )
    billing_governance.stage_billing_catalog_change(
        db,
        action="version_created",
        entity_type="offer_version",
        entity_id=version.id,
        changes=data,
        actor_id=evidence_actor_id,
        actor_type=evidence_actor_type,
        offer_id=version.offer_id,
    )
    actor_label = admission_actor_label(command.principal)
    emit_event(
        db,
        EventType.catalog_offer_version_admitted,
        {
            "schema_version": 1,
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
            "idempotency_key": key or None,
            "offer_id": str(version.offer_id),
            "offer_version_id": str(version.id),
            "version_number": version.version_number,
            "access_requirement": version.access_requirement.value,
            "authenticated_principal": actor_label,
        },
        actor=actor_label,
    )
    return AdmitOfferVersionResult(offer_version=version, replayed=False)


# --------------------------------------------------------------------------
# Read-only operations worklist of remaining unclassified rows.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnclassifiedOfferVersionRow:
    offer_version_id: UUID
    offer_id: UUID
    version_number: int
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UnclassifiedOfferVersionsWorklist:
    total_count: int
    rows: tuple[UnclassifiedOfferVersionRow, ...]
    limit: int
    offset: int


def list_unclassified_offer_versions(
    db: Session, *, limit: int = 50, offset: int = 0
) -> UnclassifiedOfferVersionsWorklist:
    """Deterministic, paginated report of every remaining unclassified row."""

    if limit < 1 or limit > 500:
        raise _error(
            "invalid_worklist_page",
            "Worklist limit must be between 1 and 500.",
            retryable=False,
        )
    if offset < 0:
        raise _error(
            "invalid_worklist_page",
            "Worklist offset cannot be negative.",
            retryable=False,
        )

    base_query = select(OfferVersion).where(
        OfferVersion.access_requirement == AccessRequirement.unclassified
    )
    total_count = int(
        db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    )
    rows = db.scalars(
        base_query.order_by(OfferVersion.created_at.asc(), OfferVersion.id.asc())
        .limit(limit)
        .offset(offset)
    ).all()
    return UnclassifiedOfferVersionsWorklist(
        total_count=total_count,
        rows=tuple(
            UnclassifiedOfferVersionRow(
                offer_version_id=row.id,
                offer_id=row.offer_id,
                version_number=row.version_number,
                name=row.name,
                created_at=_utc(row.created_at),
            )
            for row in rows
        ),
        limit=limit,
        offset=offset,
    )


# --------------------------------------------------------------------------
# Reviewed classification command.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreviewClassifyOfferAccessRequirementQuery:
    offer_version_id: UUID
    proposed_access_requirement: AccessRequirement
    review_reference: str


@dataclass(frozen=True, slots=True)
class OfferAccessRequirementClassificationPreview:
    offer_version_id: UUID
    current_access_requirement: AccessRequirement
    proposed_access_requirement: AccessRequirement
    row_updated_at: datetime
    review_reference: str
    preview_fingerprint: str
    #: True when this preview reflects an already-recorded transition
    #: (the stored fingerprint of a prior classification), not a fresh one.
    already_applied: bool = False


@dataclass(frozen=True, slots=True)
class ClassifyOfferAccessRequirementCommand:
    context: CommandContext
    query: PreviewClassifyOfferAccessRequirementQuery
    expected_preview_fingerprint: str
    #: The authenticated principal. Permission is re-verified fresh, inside
    #: this command's own transaction — never trusted from an earlier,
    #: separately-computed boolean.
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class OfferAccessRequirementClassificationResult:
    offer_version_id: UUID
    previous_access_requirement: AccessRequirement
    new_access_requirement: AccessRequirement
    replayed: bool


def _preview_fingerprint(
    *,
    offer_version_id: UUID,
    current: AccessRequirement,
    proposed: AccessRequirement,
    row_updated_at: datetime,
    review_reference: str,
) -> str:
    return _digest(
        {
            "offer_version_id": str(offer_version_id),
            "current_access_requirement": current.value,
            "proposed_access_requirement": proposed.value,
            "row_updated_at": row_updated_at.isoformat(),
            "review_reference": review_reference,
        }
    )


def preview_classify_offer_version_access_requirement(
    db: Session, query: PreviewClassifyOfferAccessRequirementQuery
) -> OfferAccessRequirementClassificationPreview:
    """Preview one explicit reclassification without changing any records.

    If this exact transition (offer version -> proposed target) was already
    recorded by a prior classification, the preview reflects that recorded
    transition and its STORED fingerprint instead of raising — this is what
    lets a genuine retry (same idempotency key, same inputs) reach the
    command's replay branch instead of being refused before it ever tries.
    A version already classified to a DIFFERENT target, or classified
    without any recorded row (e.g. admitted directly with a real value), is
    still refused: only the exact-target case previews as replayable.
    """

    review_reference = query.review_reference.strip()
    if not review_reference:
        raise _error(
            "missing_review_reference",
            "Classification requires a durable review reference.",
            retryable=False,
        )
    if len(review_reference) > _REVIEW_REFERENCE_MAX_LENGTH:
        raise _error(
            "review_reference_too_long",
            "review_reference exceeds the stored column's maximum length.",
            max_length=_REVIEW_REFERENCE_MAX_LENGTH,
            retryable=False,
        )
    if query.proposed_access_requirement not in _REAL_CLASSIFICATIONS:
        raise _error(
            "invalid_target_classification",
            "The proposed classification must be a real access requirement, "
            "never unclassified.",
            retryable=False,
        )
    version = db.get(OfferVersion, query.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(query.offer_version_id),
            retryable=False,
        )
    current = version.access_requirement

    existing = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    if existing is not None:
        if existing.new_access_requirement == query.proposed_access_requirement:
            # An already-applied transition previews as replayable ONLY when
            # the caller's review_reference matches what was actually
            # recorded. Silently substituting the STORED reference here
            # (ignoring what the caller supplied) would let a caller "replay"
            # against a review reference that was never reviewed — the
            # command's documented "same material inputs" guarantee.
            if existing.review_reference != review_reference:
                raise _error(
                    "review_reference_mismatch",
                    "This offer version was already classified under a "
                    "different review reference; a replay must supply the "
                    "exact review reference recorded on the original "
                    "classification.",
                    offer_version_id=str(version.id),
                    retryable=False,
                )
            return OfferAccessRequirementClassificationPreview(
                offer_version_id=version.id,
                current_access_requirement=existing.previous_access_requirement,
                proposed_access_requirement=existing.new_access_requirement,
                row_updated_at=_utc(version.updated_at),
                review_reference=existing.review_reference,
                preview_fingerprint=existing.preview_fingerprint,
                already_applied=True,
            )
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
            retryable=False,
        )
    if current is not AccessRequirement.unclassified:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
            retryable=False,
        )
    row_updated_at = _utc(version.updated_at)
    fingerprint = _preview_fingerprint(
        offer_version_id=version.id,
        current=current,
        proposed=query.proposed_access_requirement,
        row_updated_at=row_updated_at,
        review_reference=review_reference,
    )
    return OfferAccessRequirementClassificationPreview(
        offer_version_id=version.id,
        current_access_requirement=current,
        proposed_access_requirement=query.proposed_access_requirement,
        row_updated_at=row_updated_at,
        review_reference=review_reference,
        preview_fingerprint=fingerprint,
    )


def classify_offer_version_access_requirement(
    db: Session, command: ClassifyOfferAccessRequirementCommand
) -> OfferAccessRequirementClassificationResult:
    """Apply one reviewed, fingerprint-bound, RBAC-gated reclassification."""

    return execute_owner_command(
        db,
        definition=_CLASSIFY_COMMAND,
        context=command.context,
        operation=lambda: _classify(db, command),
    )


def _classify(
    db: Session, command: ClassifyOfferAccessRequirementCommand
) -> OfferAccessRequirementClassificationResult:
    _verify_classify_permission(db, command.authorized_system_user_id)
    actor = principal_label(command.authorized_system_user_id)

    key = (command.context.idempotency_key or "").strip()
    if not key:
        raise _error(
            "missing_idempotency_key",
            "Classification requires an idempotency key.",
            retryable=False,
        )
    if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise _error(
            "idempotency_key_too_long",
            "idempotency_key exceeds the stored column's maximum length.",
            max_length=_IDEMPOTENCY_KEY_MAX_LENGTH,
            retryable=False,
        )
    reason = (command.context.reason or "").strip()
    if not reason:
        raise _error(
            "missing_reason",
            "Classification requires a reason.",
            retryable=False,
        )

    # Serialize every command sharing this idempotency key BEFORE either one
    # can observe the other's uncommitted state. Without this, two
    # concurrent commands with the SAME key but DIFFERENT offer versions
    # each lock a different OfferVersion row below and can both pass the
    # existing_by_key lookup, with the loser hitting the unique-constraint
    # flush as a raw database error instead of a typed conflict.
    _acquire_xact_lock(db, "offer_access_requirement:classify", key)

    version = lock_for_update(db, OfferVersion, command.query.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(command.query.offer_version_id),
            retryable=False,
        )
    proposed = command.query.proposed_access_requirement
    review_reference = command.query.review_reference.strip()

    # The idempotency key is globally unique (one durable record per
    # command), so a key reused for a different version or a different
    # target is a typed conflict, checked BEFORE any write — never a raw
    # database constraint violation.
    existing_by_key = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.idempotency_key == key
        )
    )
    if existing_by_key is not None:
        exact_replay = (
            existing_by_key.offer_version_id == version.id
            and existing_by_key.new_access_requirement == proposed
            and existing_by_key.preview_fingerprint
            == command.expected_preview_fingerprint
            and existing_by_key.reason == reason
            and existing_by_key.classified_by == actor
            # A key reused with the SAME target but a DIFFERENT
            # review_reference is not a valid replay: the documented
            # "same material inputs" guarantee covers review_reference too.
            and existing_by_key.review_reference == review_reference
        )
        if exact_replay:
            return OfferAccessRequirementClassificationResult(
                offer_version_id=existing_by_key.offer_version_id,
                previous_access_requirement=existing_by_key.previous_access_requirement,
                new_access_requirement=existing_by_key.new_access_requirement,
                replayed=True,
            )
        raise _error(
            "idempotency_conflict",
            "This idempotency key was already used for a different "
            "classification command.",
            offer_version_id=str(version.id),
            retryable=False,
        )

    # No key match: a different key can never replay an already-classified
    # version. A genuine retry MUST reuse its original idempotency key —
    # that is what the key is for.
    existing_by_version = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    if existing_by_version is not None:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=existing_by_version.new_access_requirement.value,
            retryable=False,
        )
    if version.access_requirement is not AccessRequirement.unclassified:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=version.access_requirement.value,
            retryable=False,
        )

    preview = preview_classify_offer_version_access_requirement(db, command.query)
    if preview.preview_fingerprint != command.expected_preview_fingerprint:
        raise _error(
            "stale_preview",
            "The offer version changed after review; preview it again.",
            retryable=True,
        )

    # Re-verify permission again, immediately before the write, under the
    # row lock acquired above. The check at the top of this function is a
    # fast fail; only THIS second check — taken right before the mutation,
    # against the live row, inside the same transaction as the write it
    # gates — closes the window where the grant could have been revoked (or
    # the principal deactivated) between the first check and now. Ordinary
    # READ COMMITTED does not stabilize this for us just because it is the
    # same transaction: a fresh statement here reads the current committed
    # state, not a snapshot from the top of the function.
    _verify_classify_permission(db, command.authorized_system_user_id)

    version.access_requirement = proposed
    db.flush()

    classification = OfferAccessRequirementClassification(
        offer_version_id=version.id,
        previous_access_requirement=AccessRequirement.unclassified,
        new_access_requirement=proposed,
        review_reference=review_reference,
        reason=reason,
        preview_fingerprint=preview.preview_fingerprint,
        idempotency_key=key,
        classified_by=actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
    )
    db.add(classification)
    try:
        db.flush()
    except IntegrityError as exc:
        # Defense in depth for a race the advisory lock above should already
        # have serialized: a raw unique-constraint violation on
        # idempotency_key or the one-row-per-version constraint still
        # surfaces as this command's own typed conflict, never a raw
        # database error.
        raise _error(
            "idempotency_conflict",
            "This idempotency key or offer version was already used for a "
            "different classification command.",
            offer_version_id=str(version.id),
            retryable=False,
        ) from exc

    evidence = {
        "schema_version": 1,
        "command_id": str(command.context.command_id),
        "correlation_id": str(command.context.correlation_id),
        "idempotency_key": key,
        "offer_version_id": str(version.id),
        "previous_access_requirement": AccessRequirement.unclassified.value,
        "new_access_requirement": proposed.value,
        "review_reference": review_reference,
        "reason": reason,
        "authenticated_principal": actor,
    }
    stage_audit_event(
        db,
        action="offer_access_requirement_classified",
        entity_type="offer_version",
        entity_id=str(version.id),
        actor_type=AuditActorType.user,
        actor_id=actor,
        request_id=str(command.context.correlation_id),
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.catalog_offer_access_requirement_classified,
        evidence,
        actor=actor,
    )
    return OfferAccessRequirementClassificationResult(
        offer_version_id=version.id,
        previous_access_requirement=AccessRequirement.unclassified,
        new_access_requirement=proposed,
        replayed=False,
    )


__all__ = [
    "ADMISSION_SCOPE",
    "CLASSIFY_PERMISSION",
    "AdmissionPrincipal",
    "AdmitOfferVersionCommand",
    "AdmitOfferVersionResult",
    "ApiKeyPrincipal",
    "ClassifyOfferAccessRequirementCommand",
    "OWNER",
    "OfferAccessRequirementClassificationPreview",
    "OfferAccessRequirementClassificationResult",
    "OfferAccessRequirementError",
    "PreviewClassifyOfferAccessRequirementQuery",
    "StaffPrincipal",
    "SystemAdmission",
    "UnclassifiedOfferVersionRow",
    "UnclassifiedOfferVersionsWorklist",
    "admission_actor_label",
    "admit_offer_version",
    "assert_access_requirement_immutable",
    "classify_offer_version_access_requirement",
    "list_unclassified_offer_versions",
    "preview_classify_offer_version_access_requirement",
    "principal_label",
    "validate_admission_access_requirement",
]
