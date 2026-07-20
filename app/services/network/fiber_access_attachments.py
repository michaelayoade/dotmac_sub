"""Reviewed ownership of PON, ONT, and splitter-cascade attachments.

This service is the only canonical writer for active ``PonPortSplitterLink``
rows, ``SplitterCascadeLink`` rows, and the ``OntUnit.splitter_id``/
``splitter_port_id`` projection. Geometry, proximity, names, ratios, cabinets,
and legacy splitter assignments are never used to infer connectivity.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.models.fiber_access_attachment import (
    FiberAccessAttachmentDecision,
    SplitterCascadeLink,
)
from app.models.network import (
    OLTDevice,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortType,
)
from app.services.network.fiber_splitter_topology import (
    FiberSplitterTopologyError,
    RootedSplitterChain,
    lock_splitter_graph,
    resolve_splitter_chain,
    resolve_splitter_root,
    splitter_subtree_ids,
)

ACTIVE_STATUSES = ("proposed", "approved")
ATTACHMENT_TYPES = ("pon_input", "ont_output", "splitter_cascade")
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
    upstream_splitter_id: uuid.UUID | None = None
    splitter_stage: int | None = None
    cumulative_loss_db: Decimal | None = None

    def to_dict(self) -> dict[str, object | None]:
        return {
            key: (str(value) if isinstance(value, (uuid.UUID, Decimal)) else value)
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


def _decision_digest_payload(
    preview: FiberAccessAttachmentPreview, *, proposed_by: str, reason: str
) -> dict:
    payload = preview.to_dict()
    if preview.attachment_type != "splitter_cascade":
        # Preserve the stable pre-cascade digest contract for existing decision
        # types so pending proposals remain revalidatable across the migration.
        for field in (
            "upstream_splitter_id",
            "splitter_stage",
            "cumulative_loss_db",
        ):
            payload.pop(field)
    return {**payload, "proposed_by": proposed_by, "reason": reason}


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


def _active_cascade_from_output(
    db: Session, output_port_id: uuid.UUID, *, for_update: bool
) -> SplitterCascadeLink | None:
    statement = select(SplitterCascadeLink).where(
        SplitterCascadeLink.upstream_output_port_id == output_port_id,
        SplitterCascadeLink.active.is_(True),
    )
    if for_update:
        statement = statement.with_for_update()
    links = list(db.scalars(statement).all())
    if len(links) > 1:
        raise FiberAccessAttachmentError(
            "splitter output has multiple active cascade links"
        )
    return links[0] if links else None


def _active_cascade_into_splitter(
    db: Session, splitter_id: uuid.UUID, *, for_update: bool
) -> SplitterCascadeLink | None:
    statement = (
        select(SplitterCascadeLink)
        .join(
            SplitterPort,
            SplitterPort.id == SplitterCascadeLink.downstream_input_port_id,
        )
        .where(
            SplitterPort.splitter_id == splitter_id,
            SplitterCascadeLink.active.is_(True),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    links = list(db.scalars(statement).all())
    if len(links) > 1:
        raise FiberAccessAttachmentError(
            "splitter has multiple active upstream cascade links"
        )
    return links[0] if links else None


def _active_outgoing_cascades(
    db: Session, splitter_ids: set[uuid.UUID] | frozenset[uuid.UUID]
) -> list[SplitterCascadeLink]:
    if not splitter_ids:
        return []
    return list(
        db.scalars(
            select(SplitterCascadeLink)
            .join(
                SplitterPort,
                SplitterPort.id == SplitterCascadeLink.upstream_output_port_id,
            )
            .where(
                SplitterPort.splitter_id.in_(splitter_ids),
                SplitterCascadeLink.active.is_(True),
            )
        ).all()
    )


def _active_onts_in_splitters(
    db: Session, splitter_ids: set[uuid.UUID] | frozenset[uuid.UUID]
) -> list[OntUnit]:
    if not splitter_ids:
        return []
    return list(
        db.scalars(
            select(OntUnit)
            .join(SplitterPort, SplitterPort.id == OntUnit.splitter_port_id)
            .where(
                SplitterPort.splitter_id.in_(splitter_ids),
                OntUnit.is_active.is_(True),
            )
        ).all()
    )


def _active_pon_links_into_splitters(
    db: Session, splitter_ids: set[uuid.UUID] | frozenset[uuid.UUID]
) -> list[PonPortSplitterLink]:
    if not splitter_ids:
        return []
    return list(
        db.scalars(
            select(PonPortSplitterLink)
            .join(
                SplitterPort,
                SplitterPort.id == PonPortSplitterLink.splitter_port_id,
            )
            .where(
                SplitterPort.splitter_id.in_(splitter_ids),
                PonPortSplitterLink.active.is_(True),
            )
        ).all()
    )


def _resolve_rooted_chain(
    db: Session,
    splitter_id: uuid.UUID,
    *,
    pon_port_id: uuid.UUID | None = None,
    for_update: bool,
) -> RootedSplitterChain:
    try:
        if pon_port_id is None:
            chain = resolve_splitter_root(db, splitter_id, for_update=for_update)
        else:
            chain = resolve_splitter_chain(
                db,
                pon_port_id,
                splitter_id,
                for_update=for_update,
            )
        _load_active_olt(db, chain.olt_id, for_update=for_update)
        return chain
    except FiberSplitterTopologyError as exc:
        raise FiberAccessAttachmentError(str(exc)) from exc


def _subtree_ids(
    db: Session, splitter_id: uuid.UUID, *, for_update: bool
) -> frozenset[uuid.UUID]:
    try:
        return splitter_subtree_ids(db, splitter_id, for_update=for_update)
    except FiberSplitterTopologyError as exc:
        raise FiberAccessAttachmentError(str(exc)) from exc


def _assert_splitter_root_can_be_unlinked(
    db: Session, splitter_id: uuid.UUID, *, for_update: bool
) -> None:
    subtree = _subtree_ids(db, splitter_id, for_update=for_update)
    if _active_onts_in_splitters(db, subtree):
        raise FiberAccessAttachmentError(
            "cannot unlink a splitter tree with active ONT attachments"
        )
    if _active_outgoing_cascades(db, subtree):
        raise FiberAccessAttachmentError(
            "remove leaf cascade links before unlinking the PON root"
        )


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
        _assert_splitter_root_can_be_unlinked(db, splitter.id, for_update=for_update)
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
    if (
        _active_cascade_into_splitter(db, splitter.id, for_update=for_update)
        is not None
    ):
        raise FiberAccessAttachmentError(
            "splitter input is already supplied by an active cascade"
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
    if current is not None:
        _current_port, current_splitter = _load_port(
            db,
            current.splitter_port_id,
            expected_type=None,
            require_active=False,
            for_update=for_update,
        )
        if current_splitter.id != splitter.id:
            _assert_splitter_root_can_be_unlinked(
                db, current_splitter.id, for_update=for_update
            )
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
    try:
        _resolve_rooted_chain(
            db,
            splitter.id,
            pon_port_id=pon.id,
            for_update=for_update,
        )
    except FiberAccessAttachmentError as exc:
        raise FiberAccessAttachmentError(
            "ONT output must belong to the splitter attached to its PON "
            "or its exact reviewed cascade tree"
        ) from exc
    if _active_cascade_from_output(db, target.id, for_update=for_update) is not None:
        raise FiberAccessAttachmentError(
            "splitter output already supplies a downstream splitter"
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


def _preview_splitter_cascade(
    db: Session,
    action: str,
    subject_id: uuid.UUID,
    target_id: uuid.UUID | None,
    *,
    for_update: bool,
) -> FiberAccessAttachmentPreview:
    if for_update:
        try:
            lock_splitter_graph(db)
        except FiberSplitterTopologyError as exc:
            raise FiberAccessAttachmentError(str(exc)) from exc
    upstream_output, upstream_splitter = _load_port(
        db,
        subject_id,
        expected_type=SplitterPortType.output,
        require_active=(action == "attach"),
        for_update=for_update,
    )
    current = _active_cascade_from_output(db, upstream_output.id, for_update=for_update)

    if action == "detach":
        if current is None:
            raise FiberAccessAttachmentError(
                "splitter output has no active downstream cascade link"
            )
        downstream_input, downstream_splitter = _load_port(
            db,
            current.downstream_input_port_id,
            expected_type=SplitterPortType.input,
            require_active=False,
            for_update=for_update,
        )
        subtree = _subtree_ids(db, downstream_splitter.id, for_update=for_update)
        if len(subtree) > 1:
            raise FiberAccessAttachmentError(
                "remove downstream leaf cascades before detaching this link"
            )
        if _active_onts_in_splitters(db, subtree):
            raise FiberAccessAttachmentError(
                "detach active ONTs before removing their splitter cascade"
            )
        chain = _resolve_rooted_chain(db, downstream_splitter.id, for_update=for_update)
        if chain.leaf.upstream_output_port_id != upstream_output.id:
            raise FiberAccessAttachmentError(
                "active cascade does not match the exact rooted splitter chain"
            )
        if chain.leaf.cumulative_loss_db is None:
            raise FiberAccessAttachmentError(
                "cascade detach requires explicit cumulative splitter loss"
            )
        return FiberAccessAttachmentPreview(
            attachment_type="splitter_cascade",
            action=action,
            subject_id=upstream_output.id,
            target_splitter_port_id=None,
            previous_splitter_port_id=downstream_input.id,
            olt_id=chain.olt_id,
            pon_port_id=chain.pon_port_id,
            splitter_id=downstream_splitter.id,
            upstream_splitter_id=upstream_splitter.id,
            splitter_stage=chain.leaf.stage,
            cumulative_loss_db=chain.leaf.cumulative_loss_db,
        )

    assert target_id is not None
    if current is not None:
        raise FiberAccessAttachmentError(
            "splitter output already has an active downstream cascade link"
        )
    if db.scalar(
        select(OntUnit.id).where(
            OntUnit.splitter_port_id == upstream_output.id,
            OntUnit.is_active.is_(True),
        )
    ):
        raise FiberAccessAttachmentError(
            "splitter output is already attached to an active ONT"
        )

    downstream_input, downstream_splitter = _load_port(
        db,
        target_id,
        expected_type=SplitterPortType.input,
        require_active=True,
        for_update=for_update,
    )
    if upstream_splitter.id == downstream_splitter.id:
        raise FiberAccessAttachmentError("splitter cascade cannot be a self edge")
    if downstream_splitter.input_ports != 1:
        raise FiberAccessAttachmentError(
            "cascade traversal requires explicit single-input splitter inventory"
        )
    if (
        _active_cascade_into_splitter(db, downstream_splitter.id, for_update=for_update)
        is not None
    ):
        raise FiberAccessAttachmentError(
            "downstream splitter already has an active upstream cascade"
        )

    subtree = _subtree_ids(db, downstream_splitter.id, for_update=for_update)
    if upstream_splitter.id in subtree:
        raise FiberAccessAttachmentError("splitter cascade would create a cycle")
    if _active_pon_links_into_splitters(db, subtree):
        raise FiberAccessAttachmentError(
            "downstream splitter tree already has an active PON root"
        )
    if _active_onts_in_splitters(db, subtree):
        raise FiberAccessAttachmentError(
            "downstream splitter tree has unrooted active ONT attachments"
        )
    if _active_outgoing_cascades(db, subtree):
        raise FiberAccessAttachmentError(
            "attach an empty downstream splitter, then build cascades root-first"
        )

    chain = _resolve_rooted_chain(db, upstream_splitter.id, for_update=for_update)
    if chain.leaf.cumulative_loss_db is None:
        raise FiberAccessAttachmentError(
            "upstream splitter chain requires explicit insertion_loss_db"
        )
    if downstream_splitter.insertion_loss_db is None:
        raise FiberAccessAttachmentError(
            "downstream splitter requires explicit insertion_loss_db"
        )
    cumulative_loss = (
        chain.leaf.cumulative_loss_db + downstream_splitter.insertion_loss_db
    )
    return FiberAccessAttachmentPreview(
        attachment_type="splitter_cascade",
        action=action,
        subject_id=upstream_output.id,
        target_splitter_port_id=downstream_input.id,
        previous_splitter_port_id=None,
        olt_id=chain.olt_id,
        pon_port_id=chain.pon_port_id,
        splitter_id=downstream_splitter.id,
        upstream_splitter_id=upstream_splitter.id,
        splitter_stage=chain.leaf.stage + 1,
        cumulative_loss_db=cumulative_loss,
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
    if normalized_type == "ont_output":
        return _preview_ont_output(
            db,
            normalized_action,
            subject_uuid,
            target_uuid,
            for_update=for_update,
        )
    return _preview_splitter_cascade(
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
    digest_payload = _decision_digest_payload(
        preview, proposed_by=actor, reason=normalized_reason
    )
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
    if preview.target_splitter_port_id is not None:
        target_decision = db.scalar(
            select(FiberAccessAttachmentDecision).where(
                FiberAccessAttachmentDecision.target_splitter_port_id
                == preview.target_splitter_port_id,
                FiberAccessAttachmentDecision.status.in_(ACTIVE_STATUSES),
            )
        )
        if target_decision is not None:
            raise FiberAccessAttachmentError(
                "target splitter port already has an active attachment decision"
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
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise FiberAccessAttachmentError(
            "canonical attachment decision uniqueness conflict"
        ) from exc
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
    result = {
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
    if decision.attachment_type == "splitter_cascade":
        result.update(
            {
                "cumulative_loss_db": str(decision.cumulative_loss_db),
                "splitter_stage": decision.splitter_stage,
                "upstream_splitter_id": str(decision.upstream_splitter_id),
            }
        )
    return result


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

    if decision.attachment_type == "splitter_cascade":
        if decision.action == "attach":
            target_id = decision.target_splitter_port_id
            if target_id is None:
                raise FiberAccessAttachmentError(
                    "approved cascade attachment has no target input"
                )
            cascade_link = SplitterCascadeLink(
                upstream_output_port_id=decision.subject_id,
                downstream_input_port_id=target_id,
                created_by_decision_id=decision.id,
                active=True,
                notes=decision.reason,
            )
            db.add(cascade_link)
        else:
            existing_cascade = db.scalar(
                select(SplitterCascadeLink)
                .where(
                    SplitterCascadeLink.upstream_output_port_id == decision.subject_id,
                    SplitterCascadeLink.active.is_(True),
                )
                .with_for_update()
            )
            if existing_cascade is None:
                raise FiberAccessAttachmentError(
                    "splitter cascade changed before execution"
                )
            cascade_link = existing_cascade
            cascade_link.active = False
            cascade_link.retired_by_decision_id = decision.id
        db.flush()
        result.update(
            {
                "after_active": cascade_link.active,
                "after_downstream_input_port_id": (
                    str(cascade_link.downstream_input_port_id)
                    if cascade_link.active
                    else None
                ),
                "link_id": str(cascade_link.id),
                "previous_splitter_port_id": (
                    str(decision.previous_splitter_port_id)
                    if decision.previous_splitter_port_id
                    else None
                ),
                "upstream_output_port_id": str(cascade_link.upstream_output_port_id),
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
        "cumulative_loss_db": (
            str(decision.cumulative_loss_db)
            if decision.cumulative_loss_db is not None
            else None
        ),
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
        "splitter_stage": decision.splitter_stage,
        "splitter_id": str(decision.splitter_id),
        "status": decision.status,
        "subject_id": str(decision.subject_id),
        "target_splitter_port_id": (
            str(decision.target_splitter_port_id)
            if decision.target_splitter_port_id
            else None
        ),
        "upstream_splitter_id": (
            str(decision.upstream_splitter_id)
            if decision.upstream_splitter_id
            else None
        ),
    }
