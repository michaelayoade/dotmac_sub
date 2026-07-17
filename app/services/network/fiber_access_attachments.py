"""Reviewed ownership of PON-input and ONT-output fiber attachments.

This service is the only canonical writer for active ``PonPortSplitterLink``
rows and for the ``OntUnit.splitter_id``/``splitter_port_id`` projection.
Geometry, proximity, names, and legacy splitter assignments are never inputs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.fiber_access_attachment import FiberAccessAttachmentDecision
from app.models.network import (
    OLTDevice,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortType,
)

ACTIVE_STATUSES = ("proposed", "approved")
ATTACHMENT_TYPES = ("pon_input", "ont_output")
ACTIONS = ("attach", "detach")
_ModelT = TypeVar("_ModelT")


class FiberAccessAttachmentError(ValueError):
    """Raised when a reviewed access-attachment transition is invalid."""


@dataclass(frozen=True)
class FiberAccessAttachmentPreview:
    attachment_type: str
    action: str
    subject_id: uuid.UUID
    target_splitter_port_id: uuid.UUID | None
    previous_splitter_port_id: uuid.UUID | None
    olt_id: uuid.UUID
    pon_port_id: uuid.UUID
    splitter_id: uuid.UUID

    def to_dict(self) -> dict[str, str | None]:
        return {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in asdict(self).items()
        }


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberAccessAttachmentError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberAccessAttachmentError(f"{field} must be at most {limit} characters")
    return normalized


def _coerce_uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberAccessAttachmentError(f"{field} must be a UUID") from exc


def _digest(payload: dict) -> str:
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
        raise FiberAccessAttachmentError(detail)
    return value


def _active_pon_link(
    db: Session, pon_port_id: uuid.UUID, *, for_update: bool
) -> PonPortSplitterLink | None:
    statement = select(PonPortSplitterLink).where(
        PonPortSplitterLink.pon_port_id == pon_port_id,
        PonPortSplitterLink.active.is_(True),
    )
    if for_update:
        statement = statement.with_for_update()
    links = list(db.scalars(statement).all())
    if len(links) > 1:
        raise FiberAccessAttachmentError("PON has multiple active splitter-input links")
    return links[0] if links else None


def _load_active_olt(db: Session, olt_id: uuid.UUID, *, for_update: bool) -> OLTDevice:
    statement = select(OLTDevice).where(OLTDevice.id == olt_id)
    if for_update:
        statement = statement.with_for_update()
    olt = db.scalar(statement)
    if olt is None or olt.is_active is False:
        raise FiberAccessAttachmentError("authoritative OLT is missing or inactive")
    return olt


def _load_port(
    db: Session,
    port_id: uuid.UUID,
    *,
    expected_type: SplitterPortType | None,
    require_active: bool,
    for_update: bool,
) -> tuple[SplitterPort, Splitter]:
    statement = select(SplitterPort).where(SplitterPort.id == port_id)
    if for_update:
        statement = statement.with_for_update()
    port = db.scalar(statement)
    if port is None:
        raise FiberAccessAttachmentError("splitter port not found")
    if require_active and port.is_active is False:
        raise FiberAccessAttachmentError("splitter port is inactive")
    if expected_type is not None and port.port_type != expected_type:
        raise FiberAccessAttachmentError(
            f"splitter port must be an active {expected_type.value} port"
        )
    splitter = _load(
        db,
        Splitter,
        port.splitter_id,
        "splitter not found",
        for_update=for_update,
    )
    if require_active and splitter.is_active is False:
        raise FiberAccessAttachmentError("splitter is inactive")
    return port, splitter


def _normalize_request(
    attachment_type: object,
    action: object,
    subject_id: object,
    target_splitter_port_id: object | None,
) -> tuple[str, str, uuid.UUID, uuid.UUID | None]:
    normalized_type = str(attachment_type or "").strip().lower()
    normalized_action = str(action or "").strip().lower()
    if normalized_type not in ATTACHMENT_TYPES:
        raise FiberAccessAttachmentError("unsupported attachment_type")
    if normalized_action not in ACTIONS:
        raise FiberAccessAttachmentError("unsupported attachment action")
    subject_uuid = _coerce_uuid(subject_id, "subject_id")
    target_uuid = (
        _coerce_uuid(target_splitter_port_id, "target_splitter_port_id")
        if target_splitter_port_id is not None
        else None
    )
    if normalized_action == "attach" and target_uuid is None:
        raise FiberAccessAttachmentError("attach requires target_splitter_port_id")
    if normalized_action == "detach" and target_uuid is not None:
        raise FiberAccessAttachmentError(
            "detach binds the current attachment and cannot specify a target"
        )
    return normalized_type, normalized_action, subject_uuid, target_uuid


def _preview_pon_input(
    db: Session,
    action: str,
    subject_id: uuid.UUID,
    target_id: uuid.UUID | None,
    *,
    for_update: bool,
) -> FiberAccessAttachmentPreview:
    pon = _load(db, PonPort, subject_id, "PON port not found", for_update=for_update)
    if pon.is_active is False:
        raise FiberAccessAttachmentError("PON port is inactive")
    _load_active_olt(db, pon.olt_id, for_update=for_update)
    current = _active_pon_link(db, pon.id, for_update=for_update)
    if action == "detach":
        if current is None:
            raise FiberAccessAttachmentError("PON has no active splitter-input link")
        _port, splitter = _load_port(
            db,
            current.splitter_port_id,
            expected_type=None,
            require_active=False,
            for_update=for_update,
        )
        return FiberAccessAttachmentPreview(
            attachment_type="pon_input",
            action=action,
            subject_id=pon.id,
            target_splitter_port_id=None,
            previous_splitter_port_id=current.splitter_port_id,
            olt_id=pon.olt_id,
            pon_port_id=pon.id,
            splitter_id=splitter.id,
        )

    assert target_id is not None
    target, splitter = _load_port(
        db,
        target_id,
        expected_type=SplitterPortType.input,
        require_active=True,
        for_update=for_update,
    )
    occupied = db.scalar(
        select(PonPortSplitterLink).where(
            PonPortSplitterLink.splitter_port_id == target.id,
            PonPortSplitterLink.active.is_(True),
            PonPortSplitterLink.pon_port_id != pon.id,
        )
    )
    if occupied is not None:
        raise FiberAccessAttachmentError(
            "splitter input is already attached to another PON"
        )
    if current is not None and current.splitter_port_id == target.id:
        raise FiberAccessAttachmentError("PON is already attached to this input")
    return FiberAccessAttachmentPreview(
        attachment_type="pon_input",
        action=action,
        subject_id=pon.id,
        target_splitter_port_id=target.id,
        previous_splitter_port_id=(current.splitter_port_id if current else None),
        olt_id=pon.olt_id,
        pon_port_id=pon.id,
        splitter_id=splitter.id,
    )


def _validated_ont_and_pon(
    db: Session, ont_id: uuid.UUID, *, for_update: bool
) -> tuple[OntUnit, PonPort]:
    ont = _load(db, OntUnit, ont_id, "ONT not found", for_update=for_update)
    if ont.is_active is False:
        raise FiberAccessAttachmentError("ONT is inactive")
    if ont.pon_port_id is None or ont.olt_device_id is None:
        raise FiberAccessAttachmentError("ONT requires explicit PON and OLT identity")
    pon = _load(
        db,
        PonPort,
        ont.pon_port_id,
        "authoritative PON port not found",
        for_update=for_update,
    )
    if pon.is_active is False:
        raise FiberAccessAttachmentError("authoritative PON port is inactive")
    if pon.olt_id != ont.olt_device_id:
        raise FiberAccessAttachmentError(
            "ONT and PON disagree on the authoritative OLT"
        )
    _load_active_olt(db, ont.olt_device_id, for_update=for_update)
    return ont, pon


def _preview_ont_output(
    db: Session,
    action: str,
    subject_id: uuid.UUID,
    target_id: uuid.UUID | None,
    *,
    for_update: bool,
) -> FiberAccessAttachmentPreview:
    ont, pon = _validated_ont_and_pon(db, subject_id, for_update=for_update)
    current_id = ont.splitter_port_id
    if action == "detach":
        if current_id is None:
            raise FiberAccessAttachmentError("ONT has no splitter-output attachment")
        _port, splitter = _load_port(
            db,
            current_id,
            expected_type=None,
            require_active=False,
            for_update=for_update,
        )
        return FiberAccessAttachmentPreview(
            attachment_type="ont_output",
            action=action,
            subject_id=ont.id,
            target_splitter_port_id=None,
            previous_splitter_port_id=current_id,
            olt_id=pon.olt_id,
            pon_port_id=pon.id,
            splitter_id=splitter.id,
        )

    assert target_id is not None
    target, splitter = _load_port(
        db,
        target_id,
        expected_type=SplitterPortType.output,
        require_active=True,
        for_update=for_update,
    )
    pon_link = _active_pon_link(db, pon.id, for_update=for_update)
    if pon_link is None:
        raise FiberAccessAttachmentError(
            "authoritative PON has no reviewed splitter-input attachment"
        )
    input_port, _input_splitter = _load_port(
        db,
        pon_link.splitter_port_id,
        expected_type=SplitterPortType.input,
        require_active=True,
        for_update=for_update,
    )
    if input_port.splitter_id != splitter.id:
        raise FiberAccessAttachmentError(
            "ONT output must belong to the splitter attached to its PON"
        )
    occupied = db.scalar(
        select(OntUnit).where(
            OntUnit.splitter_port_id == target.id,
            OntUnit.is_active.is_(True),
            OntUnit.id != ont.id,
        )
    )
    if occupied is not None:
        raise FiberAccessAttachmentError(
            "splitter output is already attached to another active ONT"
        )
    if current_id == target.id and ont.splitter_id == splitter.id:
        raise FiberAccessAttachmentError("ONT is already attached to this output")
    return FiberAccessAttachmentPreview(
        attachment_type="ont_output",
        action=action,
        subject_id=ont.id,
        target_splitter_port_id=target.id,
        previous_splitter_port_id=current_id,
        olt_id=pon.olt_id,
        pon_port_id=pon.id,
        splitter_id=splitter.id,
    )


def preview_access_attachment(
    db: Session,
    attachment_type: str,
    action: str,
    subject_id: str | uuid.UUID,
    target_splitter_port_id: str | uuid.UUID | None = None,
    *,
    for_update: bool = False,
) -> FiberAccessAttachmentPreview:
    """Validate and return the exact before/after attachment evidence without writes."""

    normalized_type, normalized_action, subject_uuid, target_uuid = _normalize_request(
        attachment_type, action, subject_id, target_splitter_port_id
    )
    if normalized_type == "pon_input":
        return _preview_pon_input(
            db,
            normalized_action,
            subject_uuid,
            target_uuid,
            for_update=for_update,
        )
    return _preview_ont_output(
        db,
        normalized_action,
        subject_uuid,
        target_uuid,
        for_update=for_update,
    )


def propose_access_attachment(
    db: Session,
    attachment_type: str,
    action: str,
    subject_id: str | uuid.UUID,
    target_splitter_port_id: str | uuid.UUID | None = None,
    *,
    proposed_by: str,
    reason: str,
) -> FiberAccessAttachmentDecision:
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    preview = preview_access_attachment(
        db, attachment_type, action, subject_id, target_splitter_port_id
    )
    digest_payload = {
        **preview.to_dict(),
        "proposed_by": actor,
        "reason": normalized_reason,
    }
    decision_sha256 = _digest(digest_payload)
    existing = db.scalar(
        select(FiberAccessAttachmentDecision).where(
            FiberAccessAttachmentDecision.attachment_type == preview.attachment_type,
            FiberAccessAttachmentDecision.subject_id == preview.subject_id,
            FiberAccessAttachmentDecision.status.in_(ACTIVE_STATUSES),
        )
    )
    if existing is not None:
        if existing.decision_sha256 == decision_sha256:
            return existing
        raise FiberAccessAttachmentError(
            "subject already has a different active attachment decision"
        )
    if db.scalar(
        select(FiberAccessAttachmentDecision.id).where(
            FiberAccessAttachmentDecision.decision_sha256 == decision_sha256
        )
    ):
        raise FiberAccessAttachmentError(
            "this exact attachment decision is already terminal"
        )
    decision = FiberAccessAttachmentDecision(
        **asdict(preview),
        status="proposed",
        reason=normalized_reason,
        decision_sha256=decision_sha256,
        proposed_by=actor,
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


def _load_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    for_update: bool,
) -> FiberAccessAttachmentDecision:
    statement = select(FiberAccessAttachmentDecision).where(
        FiberAccessAttachmentDecision.id == _coerce_uuid(decision_id, "decision_id")
    )
    if for_update:
        statement = statement.with_for_update()
    decision = db.scalar(statement)
    if decision is None:
        raise FiberAccessAttachmentError("access attachment decision not found")
    return decision


def _assert_preview_matches(
    decision: FiberAccessAttachmentDecision,
    preview: FiberAccessAttachmentPreview,
) -> None:
    for field, value in asdict(preview).items():
        if getattr(decision, field) != value:
            raise FiberAccessAttachmentError(
                f"authoritative {field} changed after proposal"
            )


def _revalidate(
    db: Session, decision: FiberAccessAttachmentDecision
) -> FiberAccessAttachmentPreview:
    preview = preview_access_attachment(
        db,
        decision.attachment_type,
        decision.action,
        decision.subject_id,
        decision.target_splitter_port_id,
        for_update=True,
    )
    _assert_preview_matches(decision, preview)
    return preview


def approve_access_attachment(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    reviewed_by: str,
    review_notes: str,
) -> FiberAccessAttachmentDecision:
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
        raise FiberAccessAttachmentError("attachment decision is not proposed")
    if decision.proposed_by == actor:
        raise FiberAccessAttachmentError(
            "the proposer cannot review the same attachment decision"
        )
    _revalidate(db, decision)
    decision.status = "approved"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    db.commit()
    db.refresh(decision)
    return decision


def decline_access_attachment(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    reviewed_by: str,
    review_notes: str,
) -> FiberAccessAttachmentDecision:
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
        raise FiberAccessAttachmentError("attachment decision is not proposed")
    if decision.proposed_by == actor:
        raise FiberAccessAttachmentError(
            "the proposer cannot review the same attachment decision"
        )
    decision.status = "declined"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    decision.closed_reason = "attachment_decision_declined"
    db.commit()
    db.refresh(decision)
    return decision


def _set_result(
    decision: FiberAccessAttachmentDecision,
    *,
    status: str,
    actor: str,
    payload: dict,
    closed_reason: str | None = None,
) -> None:
    decision.status = status
    decision.executed_by = actor
    decision.executed_at = datetime.now(UTC)
    decision.closed_reason = closed_reason
    decision.result_payload = payload
    decision.result_sha256 = _digest(payload)


def _base_result(
    decision: FiberAccessAttachmentDecision, *, actor: str, outcome: str
) -> dict:
    return {
        "action": decision.action,
        "attachment_type": decision.attachment_type,
        "decision_id": str(decision.id),
        "executed_by": actor,
        "olt_id": str(decision.olt_id),
        "outcome": outcome,
        "pon_port_id": str(decision.pon_port_id),
        "schema_version": 1,
        "splitter_id": str(decision.splitter_id),
        "subject_id": str(decision.subject_id),
    }


def _apply_attachment(
    db: Session,
    decision: FiberAccessAttachmentDecision,
    *,
    actor: str,
) -> dict:
    result = _base_result(decision, actor=actor, outcome="applied")
    if decision.attachment_type == "pon_input":
        link = db.scalar(
            select(PonPortSplitterLink)
            .where(PonPortSplitterLink.pon_port_id == decision.pon_port_id)
            .with_for_update()
        )
        if decision.action == "attach":
            target_id = decision.target_splitter_port_id
            if target_id is None:
                raise FiberAccessAttachmentError(
                    "approved PON attachment has no target input"
                )
            if link is None:
                link = PonPortSplitterLink(
                    pon_port_id=decision.pon_port_id,
                    splitter_port_id=target_id,
                    active=True,
                )
                db.add(link)
            else:
                link.splitter_port_id = target_id
                link.active = True
        else:
            if link is None or link.active is False:
                raise FiberAccessAttachmentError(
                    "PON splitter-input attachment changed before execution"
                )
            link.active = False
        db.flush()
        result.update(
            {
                "after_active": link.active,
                "after_splitter_port_id": (
                    str(link.splitter_port_id) if link.active else None
                ),
                "link_id": str(link.id),
                "previous_splitter_port_id": (
                    str(decision.previous_splitter_port_id)
                    if decision.previous_splitter_port_id
                    else None
                ),
            }
        )
        return result

    ont = _load(db, OntUnit, decision.subject_id, "ONT not found", for_update=True)
    if decision.action == "attach":
        ont.splitter_port_id = decision.target_splitter_port_id
        ont.splitter_id = decision.splitter_id
    else:
        ont.splitter_port_id = None
        ont.splitter_id = None
    db.flush()
    result.update(
        {
            "after_splitter_id": str(ont.splitter_id) if ont.splitter_id else None,
            "after_splitter_port_id": (
                str(ont.splitter_port_id) if ont.splitter_port_id else None
            ),
            "previous_splitter_port_id": (
                str(decision.previous_splitter_port_id)
                if decision.previous_splitter_port_id
                else None
            ),
        }
    )
    return result


def execute_access_attachment(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    executed_by: str,
) -> FiberAccessAttachmentDecision:
    actor = _required_text(executed_by, "executed_by", limit=160)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status in {"applied", "closed"}:
        return decision
    if decision.status != "approved":
        raise FiberAccessAttachmentError("attachment decision is not approved")
    try:
        _revalidate(db, decision)
    except FiberAccessAttachmentError as exc:
        result = _base_result(decision, actor=actor, outcome="closed_stale")
        result["error"] = str(exc)
        _set_result(
            decision,
            status="closed",
            actor=actor,
            payload=result,
            closed_reason="authoritative_attachment_inputs_changed",
        )
        db.commit()
        db.refresh(decision)
        return decision

    try:
        result = _apply_attachment(db, decision, actor=actor)
        _set_result(decision, status="applied", actor=actor, payload=result)
        db.commit()
    except IntegrityError:
        db.rollback()
        decision = _load_decision(db, decision_id, for_update=True)
        result = _base_result(decision, actor=actor, outcome="closed_conflict")
        result["error"] = "canonical attachment uniqueness conflict"
        _set_result(
            decision,
            status="closed",
            actor=actor,
            payload=result,
            closed_reason="canonical_attachment_conflict",
        )
        db.commit()
    db.refresh(decision)
    return decision


def attachment_decision_to_dict(decision: FiberAccessAttachmentDecision) -> dict:
    return {
        "action": decision.action,
        "attachment_type": decision.attachment_type,
        "closed_reason": decision.closed_reason,
        "decision_sha256": decision.decision_sha256,
        "id": str(decision.id),
        "olt_id": str(decision.olt_id),
        "pon_port_id": str(decision.pon_port_id),
        "previous_splitter_port_id": (
            str(decision.previous_splitter_port_id)
            if decision.previous_splitter_port_id
            else None
        ),
        "proposed_by": decision.proposed_by,
        "result_payload": decision.result_payload,
        "result_sha256": decision.result_sha256,
        "reviewed_by": decision.reviewed_by,
        "splitter_id": str(decision.splitter_id),
        "status": decision.status,
        "subject_id": str(decision.subject_id),
        "target_splitter_port_id": (
            str(decision.target_splitter_port_id)
            if decision.target_splitter_port_id
            else None
        ),
    }
