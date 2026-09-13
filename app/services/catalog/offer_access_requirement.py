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
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.catalog import (
    AccessRequirement,
    OfferAccessRequirementClassification,
    OfferVersion,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "service_intent.offer_access_requirement"
CLASSIFY_PERMISSION = "catalog:offer_access_requirement:classify"

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


class OfferAccessRequirementError(DomainError):
    """Fail-closed offer-access-requirement admission/classification error."""


def _error(suffix: str, message: str, **details: object) -> OfferAccessRequirementError:
    return OfferAccessRequirementError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --------------------------------------------------------------------------
# Admission (called from OfferVersions.create) and the immutability guard
# (called from OfferVersions.update).
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
        )


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
            "invalid_worklist_page", "Worklist limit must be between 1 and 500."
        )
    if offset < 0:
        raise _error("invalid_worklist_page", "Worklist offset cannot be negative.")

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


@dataclass(frozen=True, slots=True)
class ClassifyOfferAccessRequirementCommand:
    context: CommandContext
    query: PreviewClassifyOfferAccessRequirementQuery
    expected_preview_fingerprint: str
    permission_granted: bool


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
    """Preview one explicit reclassification without changing any records."""

    review_reference = query.review_reference.strip()
    if not review_reference:
        raise _error(
            "missing_review_reference",
            "Classification requires a durable review reference.",
        )
    if query.proposed_access_requirement not in _REAL_CLASSIFICATIONS:
        raise _error(
            "invalid_target_classification",
            "The proposed classification must be a real access requirement, "
            "never unclassified.",
        )
    version = db.get(OfferVersion, query.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(query.offer_version_id),
        )
    current = version.access_requirement
    if current is not AccessRequirement.unclassified:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
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
    if command.context.scope != CLASSIFY_PERMISSION or not command.permission_granted:
        raise _error(
            "permission_denied",
            "Classification requires the catalog:offer_access_requirement:"
            "classify permission.",
        )
    key = (command.context.idempotency_key or "").strip()
    if not key:
        raise _error(
            "missing_idempotency_key",
            "Classification requires an idempotency key.",
        )

    version = lock_for_update(db, OfferVersion, command.query.offer_version_id)
    if version is None:
        raise _error(
            "offer_version_not_found",
            "The offer version does not exist.",
            offer_version_id=str(command.query.offer_version_id),
        )
    current = version.access_requirement
    proposed = command.query.proposed_access_requirement

    # At most one classification row ever exists per offer version (a DB
    # uniqueness invariant, not just an application check). Its presence is
    # the sole authority for "was this version already reviewed-classified",
    # independent of the offer version's own current value — which Release 1
    # also lets admission set directly to a real value, with no row here.
    existing = db.scalar(
        select(OfferAccessRequirementClassification).where(
            OfferAccessRequirementClassification.offer_version_id == version.id
        )
    )
    if existing is not None:
        if (
            existing.idempotency_key == key
            and existing.new_access_requirement == proposed
        ):
            return OfferAccessRequirementClassificationResult(
                offer_version_id=version.id,
                previous_access_requirement=existing.previous_access_requirement,
                new_access_requirement=existing.new_access_requirement,
                replayed=True,
            )
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
        )
    if current is not AccessRequirement.unclassified:
        raise _error(
            "already_classified",
            "Only an unclassified offer version may be reviewed-classified; "
            "real-to-real and real-to-unclassified changes are refused.",
            offer_version_id=str(version.id),
            current_access_requirement=current.value,
        )

    preview = preview_classify_offer_version_access_requirement(db, command.query)
    if preview.preview_fingerprint != command.expected_preview_fingerprint:
        raise _error(
            "stale_preview",
            "The offer version changed after review; preview it again.",
        )

    reason = (command.context.reason or "").strip()
    review_reference = command.query.review_reference.strip()
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
        classified_by=command.context.actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
    )
    db.add(classification)
    db.flush()

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
        "authenticated_principal": command.context.actor,
    }
    stage_audit_event(
        db,
        action="offer_access_requirement_classified",
        entity_type="offer_version",
        entity_id=str(version.id),
        actor_type=AuditActorType.user,
        actor_id=command.context.actor,
        request_id=str(command.context.correlation_id),
        metadata=evidence,
    )
    emit_event(
        db,
        EventType.catalog_offer_access_requirement_classified,
        evidence,
        actor=command.context.actor,
    )
    return OfferAccessRequirementClassificationResult(
        offer_version_id=version.id,
        previous_access_requirement=AccessRequirement.unclassified,
        new_access_requirement=proposed,
        replayed=False,
    )


__all__ = [
    "CLASSIFY_PERMISSION",
    "ClassifyOfferAccessRequirementCommand",
    "OWNER",
    "OfferAccessRequirementClassificationPreview",
    "OfferAccessRequirementClassificationResult",
    "OfferAccessRequirementError",
    "PreviewClassifyOfferAccessRequirementQuery",
    "UnclassifiedOfferVersionRow",
    "UnclassifiedOfferVersionsWorklist",
    "assert_access_requirement_immutable",
    "classify_offer_version_access_requirement",
    "list_unclassified_offer_versions",
    "preview_classify_offer_version_access_requirement",
    "validate_admission_access_requirement",
]
