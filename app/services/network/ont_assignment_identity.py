"""Reviewed repair owner for ONT assignment electronic identity.

The owner binds explicit assignment, subscription, PON, and OLT identifiers.
Subscriber/address/name fallbacks and imported registration guesses are never
accepted as repair inputs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort
from app.models.ont_assignment_identity import OntAssignmentIdentityDecision
from app.services.network_subscriber_bridge import (
    AssignmentSubscriptionSnapshot,
    assignment_subscription_snapshot,
)

ACTIVE_STATUSES = ("proposed", "approved")
_ModelT = TypeVar("_ModelT")


class OntAssignmentIdentityError(ValueError):
    """Raised when an electronic identity repair transition is invalid."""


@dataclass(frozen=True)
class OntAssignmentIdentityPreview:
    action: str
    primary_assignment_id: uuid.UUID
    ont_unit_id: uuid.UUID
    target_subscription_id: uuid.UUID | None
    target_subscriber_id: uuid.UUID | None
    target_pon_port_id: uuid.UUID | None
    target_olt_id: uuid.UUID | None
    duplicate_assignment_ids: tuple[uuid.UUID, ...]
    input_snapshot: dict[str, object]
    input_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "duplicate_assignment_ids": [
                str(value) for value in self.duplicate_assignment_ids
            ],
            "input_sha256": self.input_sha256,
            "input_snapshot": self.input_snapshot,
            "ont_unit_id": str(self.ont_unit_id),
            "primary_assignment_id": str(self.primary_assignment_id),
            "target_olt_id": str(self.target_olt_id) if self.target_olt_id else None,
            "target_pon_port_id": (
                str(self.target_pon_port_id) if self.target_pon_port_id else None
            ),
            "target_subscriber_id": (
                str(self.target_subscriber_id) if self.target_subscriber_id else None
            ),
            "target_subscription_id": (
                str(self.target_subscription_id)
                if self.target_subscription_id
                else None
            ),
        }


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OntAssignmentIdentityError(f"{field} is required")
    if len(normalized) > limit:
        raise OntAssignmentIdentityError(f"{field} must be at most {limit} characters")
    return normalized


def _coerce_uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise OntAssignmentIdentityError(f"{field} must be a UUID") from exc


def _optional_uuid(value: object | None, field: str) -> uuid.UUID | None:
    return _coerce_uuid(value, field) if value is not None else None


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load(
    db: Session,
    model: type[_ModelT],
    object_id: uuid.UUID,
    detail: str,
    *,
    for_update: bool,
) -> _ModelT:
    statement: Select[tuple[_ModelT]] = select(model).where(
        model.id == object_id  # type: ignore[attr-defined]
    )
    if for_update:
        statement = statement.with_for_update()
    value = db.scalar(statement)
    if value is None:
        raise OntAssignmentIdentityError(detail)
    return value


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _assignment_snapshot(assignment: OntAssignment) -> dict[str, object]:
    return {
        "active": assignment.active,
        "assigned_at": _timestamp(assignment.assigned_at),
        "id": str(assignment.id),
        "ont_unit_id": str(assignment.ont_unit_id),
        "pon_port_id": (
            str(assignment.pon_port_id) if assignment.pon_port_id else None
        ),
        "release_reason": assignment.release_reason,
        "released_at": _timestamp(assignment.released_at),
        "subscriber_id": (
            str(assignment.subscriber_id) if assignment.subscriber_id else None
        ),
        "subscription_id": (
            str(assignment.subscription_id) if assignment.subscription_id else None
        ),
    }


def _ont_snapshot(ont: OntUnit) -> dict[str, object]:
    return {
        "id": str(ont.id),
        "is_active": ont.is_active,
        "olt_device_id": str(ont.olt_device_id) if ont.olt_device_id else None,
        "pon_port_id": str(ont.pon_port_id) if ont.pon_port_id else None,
    }


def _subscription_snapshot(
    subscription: AssignmentSubscriptionSnapshot,
) -> dict[str, object]:
    return {
        "id": str(subscription.id),
        "offer_id": str(subscription.offer_id),
        "status": subscription.status,
        "subscriber_id": str(subscription.subscriber_id),
    }


def _pon_snapshot(pon: PonPort, olt: OLTDevice) -> dict[str, object]:
    return {
        "olt": {"id": str(olt.id), "is_active": olt.is_active},
        "pon": {
            "id": str(pon.id),
            "is_active": pon.is_active,
            "olt_id": str(pon.olt_id),
        },
    }


def _normalize_duplicates(values: object | None) -> tuple[uuid.UUID, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raw_values = [part.strip() for part in str(values).split(",") if part.strip()]
    elif isinstance(values, Iterable):
        raw_values = list(values)
    else:
        raise OntAssignmentIdentityError(
            "duplicate_assignment_ids must be a sequence of UUIDs"
        )
    normalized = tuple(
        sorted(
            {_coerce_uuid(value, "duplicate_assignment_id") for value in raw_values},
            key=str,
        )
    )
    return normalized


def _active_conflicts(
    db: Session,
    *,
    primary: OntAssignment,
    target_subscription_id: uuid.UUID,
    for_update: bool,
) -> list[OntAssignment]:
    statement = select(OntAssignment).where(
        OntAssignment.id != primary.id,
        OntAssignment.active.is_(True),
        or_(
            OntAssignment.ont_unit_id == primary.ont_unit_id,
            OntAssignment.subscription_id == target_subscription_id,
        ),
    )
    if for_update:
        statement = statement.with_for_update()
    return list(db.scalars(statement).all())


def active_assignment_identity_conflict_ids(
    db: Session,
    primary_assignment_id: str | uuid.UUID,
    target_subscription_id: str | uuid.UUID,
    *,
    for_update: bool = False,
) -> tuple[uuid.UUID, ...]:
    """Return the complete exact conflict set for an explicit repair target.

    This is deterministic conflict enumeration, not identity inference: callers
    must still supply the primary assignment and target subscription IDs.
    """

    primary = _load(
        db,
        OntAssignment,
        _coerce_uuid(primary_assignment_id, "primary_assignment_id"),
        "primary ONT assignment not found",
        for_update=for_update,
    )
    if primary.active is False:
        raise OntAssignmentIdentityError("primary ONT assignment is not active")
    subscription_id = _coerce_uuid(target_subscription_id, "target_subscription_id")
    return tuple(
        sorted(
            (
                row.id
                for row in _active_conflicts(
                    db,
                    primary=primary,
                    target_subscription_id=subscription_id,
                    for_update=for_update,
                )
            ),
            key=str,
        )
    )


def preview_assignment_identity_repair(
    db: Session,
    action: str,
    primary_assignment_id: str | uuid.UUID,
    *,
    target_subscription_id: str | uuid.UUID | None = None,
    target_pon_port_id: str | uuid.UUID | None = None,
    target_olt_id: str | uuid.UUID | None = None,
    duplicate_assignment_ids: object | None = None,
    for_update: bool = False,
) -> OntAssignmentIdentityPreview:
    """Return exact repair inputs and conflicts without writing."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"canonicalize", "deactivate"}:
        raise OntAssignmentIdentityError("unsupported assignment identity action")
    primary_id = _coerce_uuid(primary_assignment_id, "primary_assignment_id")
    primary = _load(
        db,
        OntAssignment,
        primary_id,
        "primary ONT assignment not found",
        for_update=for_update,
    )
    if primary.active is False:
        raise OntAssignmentIdentityError("primary ONT assignment is not active")
    ont = _load(
        db,
        OntUnit,
        primary.ont_unit_id,
        "primary assignment ONT not found",
        for_update=for_update,
    )
    duplicates = _normalize_duplicates(duplicate_assignment_ids)

    if normalized_action == "deactivate":
        if (
            any(
                value is not None
                for value in (
                    target_subscription_id,
                    target_pon_port_id,
                    target_olt_id,
                )
            )
            or duplicates
        ):
            raise OntAssignmentIdentityError(
                "deactivate cannot specify targets or duplicate assignments"
            )
        snapshot: dict[str, object] = {
            "duplicates": [],
            "ont": _ont_snapshot(ont),
            "primary_assignment": _assignment_snapshot(primary),
        }
        return OntAssignmentIdentityPreview(
            action=normalized_action,
            primary_assignment_id=primary.id,
            ont_unit_id=ont.id,
            target_subscription_id=None,
            target_subscriber_id=None,
            target_pon_port_id=None,
            target_olt_id=None,
            duplicate_assignment_ids=(),
            input_snapshot=snapshot,
            input_sha256=_digest(snapshot),
        )

    subscription_id = _optional_uuid(target_subscription_id, "target_subscription_id")
    pon_id = _optional_uuid(target_pon_port_id, "target_pon_port_id")
    olt_id = _optional_uuid(target_olt_id, "target_olt_id")
    if subscription_id is None or pon_id is None or olt_id is None:
        raise OntAssignmentIdentityError(
            "canonicalize requires target subscription, PON, and OLT IDs"
        )
    subscription = assignment_subscription_snapshot(
        db, subscription_id, for_update=for_update
    )
    if subscription is None:
        raise OntAssignmentIdentityError("target subscription not found")
    if not subscription.assignment_eligible:
        raise OntAssignmentIdentityError(
            "target subscription is terminal and cannot own an active ONT assignment"
        )
    pon = _load(db, PonPort, pon_id, "target PON port not found", for_update=for_update)
    olt = _load(db, OLTDevice, olt_id, "target OLT not found", for_update=for_update)
    if pon.is_active is False or olt.is_active is False:
        raise OntAssignmentIdentityError("target PON and OLT must be active")
    if pon.olt_id != olt.id:
        raise OntAssignmentIdentityError("target PON does not belong to target OLT")

    conflicts = _active_conflicts(
        db,
        primary=primary,
        target_subscription_id=subscription.id,
        for_update=for_update,
    )
    conflict_ids = tuple(sorted((row.id for row in conflicts), key=str))
    if duplicates != conflict_ids:
        raise OntAssignmentIdentityError(
            "duplicate_assignment_ids must exactly cover every active ONT or "
            "subscription conflict"
        )
    if primary.id in duplicates:
        raise OntAssignmentIdentityError(
            "primary assignment cannot also be a duplicate"
        )
    already_exact = (
        primary.subscription_id == subscription.id
        and primary.subscriber_id == subscription.subscriber_id
        and primary.pon_port_id == pon.id
        and ont.pon_port_id == pon.id
        and ont.olt_device_id == olt.id
        and ont.is_active is True
        and not duplicates
    )
    if already_exact:
        raise OntAssignmentIdentityError(
            "assignment and ONT identity are already canonical"
        )
    snapshot = {
        "duplicates": [
            _assignment_snapshot(row)
            for row in sorted(conflicts, key=lambda row: str(row.id))
        ],
        "ont": _ont_snapshot(ont),
        "primary_assignment": _assignment_snapshot(primary),
        "target": {
            **_pon_snapshot(pon, olt),
            "subscription": _subscription_snapshot(subscription),
        },
    }
    return OntAssignmentIdentityPreview(
        action=normalized_action,
        primary_assignment_id=primary.id,
        ont_unit_id=ont.id,
        target_subscription_id=subscription.id,
        target_subscriber_id=subscription.subscriber_id,
        target_pon_port_id=pon.id,
        target_olt_id=olt.id,
        duplicate_assignment_ids=duplicates,
        input_snapshot=snapshot,
        input_sha256=_digest(snapshot),
    )


def propose_assignment_identity_repair(
    db: Session,
    action: str,
    primary_assignment_id: str | uuid.UUID,
    *,
    proposed_by: str,
    reason: str,
    target_subscription_id: str | uuid.UUID | None = None,
    target_pon_port_id: str | uuid.UUID | None = None,
    target_olt_id: str | uuid.UUID | None = None,
    duplicate_assignment_ids: object | None = None,
    expected_input_sha256: str | None = None,
    proposal_batch_id: str | uuid.UUID | None = None,
    proposal_batch_row_number: int | None = None,
    commit: bool = True,
) -> OntAssignmentIdentityDecision:
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    normalized_batch_id = _optional_uuid(proposal_batch_id, "proposal_batch_id")
    if (normalized_batch_id is None) != (proposal_batch_row_number is None):
        raise OntAssignmentIdentityError(
            "proposal_batch_id and proposal_batch_row_number must be supplied together"
        )
    if proposal_batch_row_number is not None and proposal_batch_row_number < 1:
        raise OntAssignmentIdentityError(
            "proposal_batch_row_number must be greater than zero"
        )
    preview = preview_assignment_identity_repair(
        db,
        action,
        primary_assignment_id,
        target_subscription_id=target_subscription_id,
        target_pon_port_id=target_pon_port_id,
        target_olt_id=target_olt_id,
        duplicate_assignment_ids=duplicate_assignment_ids,
    )
    if (
        expected_input_sha256 is not None
        and preview.input_sha256
        != _required_text(
            expected_input_sha256, "expected_input_sha256", limit=64
        ).lower()
    ):
        raise OntAssignmentIdentityError(
            "authoritative assignment identity inputs changed after preview"
        )
    digest_payload = {
        **preview.to_dict(),
        "proposed_by": actor,
        "reason": normalized_reason,
    }
    if normalized_batch_id is not None:
        digest_payload["proposal_batch"] = {
            "id": str(normalized_batch_id),
            "row_number": proposal_batch_row_number,
        }
    decision_sha256 = _digest(digest_payload)
    existing = db.scalar(
        select(OntAssignmentIdentityDecision).where(
            OntAssignmentIdentityDecision.primary_assignment_id
            == preview.primary_assignment_id,
            OntAssignmentIdentityDecision.status.in_(ACTIVE_STATUSES),
        )
    )
    if existing is not None:
        if existing.decision_sha256 == decision_sha256:
            return existing
        raise OntAssignmentIdentityError(
            "primary assignment already has a different active identity decision"
        )
    if db.scalar(
        select(OntAssignmentIdentityDecision.id).where(
            OntAssignmentIdentityDecision.decision_sha256 == decision_sha256
        )
    ):
        raise OntAssignmentIdentityError(
            "this exact assignment identity decision is already terminal"
        )
    decision = OntAssignmentIdentityDecision(
        action=preview.action,
        status="proposed",
        primary_assignment_id=preview.primary_assignment_id,
        ont_unit_id=preview.ont_unit_id,
        target_subscription_id=preview.target_subscription_id,
        target_subscriber_id=preview.target_subscriber_id,
        target_pon_port_id=preview.target_pon_port_id,
        target_olt_id=preview.target_olt_id,
        duplicate_assignment_ids=[
            str(value) for value in preview.duplicate_assignment_ids
        ],
        input_snapshot=preview.input_snapshot,
        input_sha256=preview.input_sha256,
        reason=normalized_reason,
        decision_sha256=decision_sha256,
        proposed_by=actor,
        proposal_batch_id=normalized_batch_id,
        proposal_batch_row_number=proposal_batch_row_number,
    )
    db.add(decision)
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def _load_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    for_update: bool,
) -> OntAssignmentIdentityDecision:
    statement = select(OntAssignmentIdentityDecision).where(
        OntAssignmentIdentityDecision.id == _coerce_uuid(decision_id, "decision_id")
    )
    if for_update:
        statement = statement.with_for_update()
    decision = db.scalar(statement)
    if decision is None:
        raise OntAssignmentIdentityError("assignment identity decision not found")
    return decision


def _revalidate(
    db: Session, decision: OntAssignmentIdentityDecision
) -> OntAssignmentIdentityPreview:
    preview = preview_assignment_identity_repair(
        db,
        decision.action,
        decision.primary_assignment_id,
        target_subscription_id=decision.target_subscription_id,
        target_pon_port_id=decision.target_pon_port_id,
        target_olt_id=decision.target_olt_id,
        duplicate_assignment_ids=decision.duplicate_assignment_ids,
        for_update=True,
    )
    if preview.input_sha256 != decision.input_sha256:
        raise OntAssignmentIdentityError(
            "authoritative assignment identity inputs changed after proposal"
        )
    return preview


def approve_assignment_identity_repair(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    reviewed_by: str,
    review_notes: str,
    commit: bool = True,
) -> OntAssignmentIdentityDecision:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status != "proposed":
        if (
            decision.status == "approved"
            and decision.reviewed_by == actor
            and decision.review_notes == notes
        ):
            return decision
        raise OntAssignmentIdentityError("assignment identity decision is not proposed")
    if decision.proposed_by == actor:
        raise OntAssignmentIdentityError(
            "the proposer cannot review the same assignment identity decision"
        )
    _revalidate(db, decision)
    decision.status = "approved"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def decline_assignment_identity_repair(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    reviewed_by: str,
    review_notes: str,
    commit: bool = True,
) -> OntAssignmentIdentityDecision:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status != "proposed":
        if (
            decision.status == "declined"
            and decision.reviewed_by == actor
            and decision.review_notes == notes
        ):
            return decision
        raise OntAssignmentIdentityError("assignment identity decision is not proposed")
    if decision.proposed_by == actor:
        raise OntAssignmentIdentityError(
            "the proposer cannot review the same assignment identity decision"
        )
    decision.status = "declined"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    decision.closed_reason = "assignment_identity_decision_declined"
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def _base_result(
    decision: OntAssignmentIdentityDecision, *, actor: str, outcome: str
) -> dict[str, object]:
    return {
        "action": decision.action,
        "decision_id": str(decision.id),
        "executed_by": actor,
        "input_sha256": decision.input_sha256,
        "ont_unit_id": str(decision.ont_unit_id),
        "outcome": outcome,
        "primary_assignment_id": str(decision.primary_assignment_id),
        "schema_version": 1,
    }


def _set_result(
    decision: OntAssignmentIdentityDecision,
    *,
    status: str,
    actor: str,
    payload: dict[str, object],
    closed_reason: str | None = None,
) -> None:
    decision.status = status
    decision.executed_by = actor
    decision.executed_at = datetime.now(UTC)
    decision.closed_reason = closed_reason
    decision.result_payload = payload
    decision.result_sha256 = _digest(payload)


def _apply(
    db: Session,
    decision: OntAssignmentIdentityDecision,
    *,
    actor: str,
) -> dict[str, object]:
    primary = _load(
        db,
        OntAssignment,
        decision.primary_assignment_id,
        "primary ONT assignment not found",
        for_update=True,
    )
    ont = _load(db, OntUnit, decision.ont_unit_id, "ONT not found", for_update=True)
    now = datetime.now(UTC)
    result = _base_result(decision, actor=actor, outcome="applied")
    if decision.action == "deactivate":
        primary.active = False
        primary.released_at = now
        primary.release_reason = "identity_repair_deactivated"
        db.flush()
        result["primary_after"] = _assignment_snapshot(primary)
        return result

    target_subscription_id = decision.target_subscription_id
    target_subscriber_id = decision.target_subscriber_id
    target_pon_port_id = decision.target_pon_port_id
    target_olt_id = decision.target_olt_id
    if (
        target_subscription_id is None
        or target_subscriber_id is None
        or target_pon_port_id is None
        or target_olt_id is None
    ):
        raise OntAssignmentIdentityError("approved canonical repair lacks targets")
    primary.subscription_id = target_subscription_id
    primary.subscriber_id = target_subscriber_id
    primary.pon_port_id = target_pon_port_id
    primary.active = True
    primary.assigned_at = primary.assigned_at or now
    primary.released_at = None
    primary.release_reason = None
    ont.pon_port_id = target_pon_port_id
    ont.olt_device_id = target_olt_id
    ont.is_active = True

    duplicate_rows: list[OntAssignment] = []
    for duplicate_id in _normalize_duplicates(decision.duplicate_assignment_ids):
        duplicate = _load(
            db,
            OntAssignment,
            duplicate_id,
            "duplicate ONT assignment not found",
            for_update=True,
        )
        duplicate.active = False
        duplicate.released_at = now
        duplicate.release_reason = "identity_repair_duplicate"
        duplicate_rows.append(duplicate)
    db.flush()
    result.update(
        {
            "deactivated_duplicates": [
                _assignment_snapshot(row)
                for row in sorted(duplicate_rows, key=lambda row: str(row.id))
            ],
            "ont_after": _ont_snapshot(ont),
            "primary_after": _assignment_snapshot(primary),
            "target_olt_id": str(target_olt_id),
            "target_pon_port_id": str(target_pon_port_id),
            "target_subscriber_id": str(target_subscriber_id),
            "target_subscription_id": str(target_subscription_id),
        }
    )
    return result


def execute_assignment_identity_repair(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    executed_by: str,
) -> OntAssignmentIdentityDecision:
    actor = _required_text(executed_by, "executed_by", limit=160)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status in {"applied", "closed"}:
        return decision
    if decision.status != "approved":
        raise OntAssignmentIdentityError("assignment identity decision is not approved")
    try:
        _revalidate(db, decision)
    except OntAssignmentIdentityError as exc:
        result = _base_result(decision, actor=actor, outcome="closed_stale")
        result["error"] = str(exc)
        _set_result(
            decision,
            status="closed",
            actor=actor,
            payload=result,
            closed_reason="authoritative_assignment_identity_inputs_changed",
        )
        db.commit()
        db.refresh(decision)
        return decision

    try:
        result = _apply(db, decision, actor=actor)
        _set_result(decision, status="applied", actor=actor, payload=result)
        db.commit()
    except IntegrityError:
        db.rollback()
        decision = _load_decision(db, decision_id, for_update=True)
        result = _base_result(decision, actor=actor, outcome="closed_conflict")
        result["error"] = "canonical assignment identity uniqueness conflict"
        _set_result(
            decision,
            status="closed",
            actor=actor,
            payload=result,
            closed_reason="canonical_assignment_identity_conflict",
        )
        db.commit()
    db.refresh(decision)
    return decision


def assignment_identity_decision_to_dict(
    decision: OntAssignmentIdentityDecision,
) -> dict[str, object]:
    return {
        "action": decision.action,
        "closed_reason": decision.closed_reason,
        "decision_sha256": decision.decision_sha256,
        "duplicate_assignment_ids": decision.duplicate_assignment_ids,
        "id": str(decision.id),
        "input_sha256": decision.input_sha256,
        "ont_unit_id": str(decision.ont_unit_id),
        "primary_assignment_id": str(decision.primary_assignment_id),
        "proposal_batch_id": (
            str(decision.proposal_batch_id) if decision.proposal_batch_id else None
        ),
        "proposal_batch_row_number": decision.proposal_batch_row_number,
        "proposed_by": decision.proposed_by,
        "result_payload": decision.result_payload,
        "result_sha256": decision.result_sha256,
        "reviewed_by": decision.reviewed_by,
        "status": decision.status,
        "target_olt_id": str(decision.target_olt_id)
        if decision.target_olt_id
        else None,
        "target_pon_port_id": (
            str(decision.target_pon_port_id) if decision.target_pon_port_id else None
        ),
        "target_subscriber_id": (
            str(decision.target_subscriber_id)
            if decision.target_subscriber_id
            else None
        ),
        "target_subscription_id": (
            str(decision.target_subscription_id)
            if decision.target_subscription_id
            else None
        ),
    }
