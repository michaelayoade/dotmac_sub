"""Reviewed ownership of racks, patches, splices, and exact core continuity.

Names, labels, geometry, and the legacy ``FiberSegment.fiber_strand_id`` scalar
never create physical continuity. Canonical links bind exact connector, strand,
strand-end, rack/panel, closure, and reviewed-decision identities.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fiber_change_request import FiberChangeRequestOperation
from app.models.fiber_physical import (
    FiberConnectorPort,
    FiberCoreSplice,
    FiberPatchCord,
    FiberPatchPanel,
    FiberPhysicalLinkDecision,
    FiberRack,
    FiberStrandTermination,
)
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
    FiberSpliceTray,
    FiberStrand,
    FiberStrandStatus,
    FiberTerminationPoint,
    ODNEndpointType,
    OntUnit,
    PonPort,
    Splitter,
    SplitterPort,
)
from app.models.network_monitoring import NetworkDevice, PopSite

LINK_TYPES = ("core_splice", "strand_termination", "patch_cord")
ACTIONS = ("connect", "disconnect")
ACTIVE_DECISION_STATUSES = ("proposed", "approved")
STRAND_ENDS = ("a", "b")
PANEL_TYPES = ("odf", "patch_panel", "splice_panel")
CONNECTOR_TYPES = ("sc", "lc", "fc", "st")
POLISH_TYPES = ("apc", "upc", "pc")
FIBER_MODES = ("single_mode", "multi_mode")


class FiberPhysicalContinuityError(ValueError):
    """Raised when physical inventory or continuity would become invalid."""


@dataclass(frozen=True)
class FiberPhysicalLinkPreview:
    link_type: str
    action: str
    target_link_id: uuid.UUID | None = None
    first_strand_id: uuid.UUID | None = None
    first_strand_end: str | None = None
    second_strand_id: uuid.UUID | None = None
    second_strand_end: str | None = None
    connector_port_id: uuid.UUID | None = None
    first_connector_port_id: uuid.UUID | None = None
    second_connector_port_id: uuid.UUID | None = None
    splice_closure_id: uuid.UUID | None = None
    splice_tray_id: uuid.UUID | None = None
    position: int | None = None
    splice_type: str | None = None
    label: str | None = None
    assembly_label: str | None = None
    length_m: Decimal | None = None
    insertion_loss_db: Decimal | None = None

    def to_dict(self) -> dict[str, object | None]:
        return {
            key: (str(value) if isinstance(value, (uuid.UUID, Decimal)) else value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class FiberCoreContinuityHop:
    kind: str
    asset_id: uuid.UUID
    label: str
    evidence_refs: tuple[str, ...]
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class FiberCoreContinuityGap:
    code: str
    message: str
    after_asset_id: uuid.UUID | None = None


@dataclass(frozen=True)
class FiberCoreContinuityResult:
    hops: tuple[FiberCoreContinuityHop, ...]
    gaps: tuple[FiberCoreContinuityGap, ...]
    logical_segment_ids: tuple[uuid.UUID, ...]
    evidence_sha256: str

    @property
    def complete(self) -> bool:
        return not self.gaps

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["complete"] = self.complete
        payload["schema_version"] = 1
        return payload


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            default=str,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise FiberPhysicalContinuityError(f"{field} must be a UUID") from exc


def _optional_uuid(value: object | None, field: str) -> uuid.UUID | None:
    return None if value in (None, "") else _uuid(value, field)


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberPhysicalContinuityError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberPhysicalContinuityError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _optional_text(value: object | None, *, limit: int) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if len(normalized) > limit:
        raise FiberPhysicalContinuityError(
            f"text value must be at most {limit} characters"
        )
    return normalized


def _decimal(
    value: object | None, field: str, *, positive: bool = False
) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FiberPhysicalContinuityError(f"{field} must be numeric") from exc
    if (positive and result <= 0) or (not positive and result < 0):
        comparison = "positive" if positive else "non-negative"
        raise FiberPhysicalContinuityError(f"{field} must be {comparison}")
    return result


def _active(row: object | None) -> bool:
    return bool(row is not None and getattr(row, "is_active", True))


def _validate_rack(
    db: Session, rack: FiberRack, *, omit_id: uuid.UUID | None = None
) -> None:
    hosts = (
        (PopSite, rack.pop_site_id, "POP"),
        (FdhCabinet, rack.fdh_cabinet_id, "FDH cabinet"),
        (FiberAccessPoint, rack.fiber_access_point_id, "fiber access point"),
        (FiberSpliceClosure, rack.splice_closure_id, "splice closure"),
    )
    selected = [(model, value, label) for model, value, label in hosts if value]
    if len(selected) != 1:
        raise FiberPhysicalContinuityError(
            "fiber rack requires exactly one canonical host"
        )
    model, host_id, label = selected[0]
    if not _active(db.get(model, host_id)):
        raise FiberPhysicalContinuityError(
            f"active fiber rack requires an active {label} host"
        )
    if rack.rack_units <= 0:
        raise FiberPhysicalContinuityError("rack_units must be positive")
    if rack.is_active is False or rack.id is None:
        return
    panels = list(
        db.scalars(
            select(FiberPatchPanel).where(
                FiberPatchPanel.rack_id == rack.id,
                FiberPatchPanel.is_active.is_(True),
            )
        ).all()
    )
    if any(
        panel.rack_unit_start + panel.rack_unit_height - 1 > rack.rack_units
        for panel in panels
    ):
        raise FiberPhysicalContinuityError(
            "rack_units cannot exclude an active mounted patch panel"
        )


def _validate_panel(
    db: Session, panel: FiberPatchPanel, *, omit_id: uuid.UUID | None = None
) -> None:
    rack = db.get(FiberRack, panel.rack_id)
    if rack is None or not rack.is_active:
        raise FiberPhysicalContinuityError(
            "active patch panel requires an active canonical fiber rack"
        )
    if panel.panel_type not in PANEL_TYPES:
        raise FiberPhysicalContinuityError("unsupported patch panel type")
    if panel.connector_type not in CONNECTOR_TYPES:
        if panel.connector_type in {"mpo", "mtp"}:
            raise FiberPhysicalContinuityError(
                "MPO/MTP panel inventory requires an explicit assembly and lane model"
            )
        raise FiberPhysicalContinuityError("unsupported panel connector type")
    if panel.polish_type not in POLISH_TYPES:
        raise FiberPhysicalContinuityError("unsupported panel polish type")
    if panel.fiber_mode not in FIBER_MODES:
        raise FiberPhysicalContinuityError("unsupported panel fiber mode")
    end_unit = panel.rack_unit_start + panel.rack_unit_height - 1
    if panel.rack_unit_start <= 0 or panel.rack_unit_height <= 0:
        raise FiberPhysicalContinuityError("patch panel rack units must be positive")
    if end_unit > rack.rack_units:
        raise FiberPhysicalContinuityError("patch panel exceeds rack unit capacity")
    if panel.port_capacity <= 0:
        raise FiberPhysicalContinuityError("patch panel port_capacity must be positive")
    statement = select(FiberPatchPanel).where(
        FiberPatchPanel.rack_id == rack.id,
        FiberPatchPanel.is_active.is_(True),
        FiberPatchPanel.rack_unit_start <= end_unit,
        (
            FiberPatchPanel.rack_unit_start + FiberPatchPanel.rack_unit_height - 1
            >= panel.rack_unit_start
        ),
    )
    if omit_id is not None:
        statement = statement.where(FiberPatchPanel.id != omit_id)
    if panel.is_active and db.scalar(statement) is not None:
        raise FiberPhysicalContinuityError(
            "patch panel rack-unit range overlaps another active panel"
        )
    if panel.id is not None:
        maximum_port = db.scalar(
            select(FiberConnectorPort.port_number)
            .where(
                FiberConnectorPort.patch_panel_id == panel.id,
                FiberConnectorPort.is_active.is_(True),
            )
            .order_by(FiberConnectorPort.port_number.desc())
            .limit(1)
        )
        if maximum_port is not None and maximum_port > panel.port_capacity:
            raise FiberPhysicalContinuityError(
                "patch panel capacity cannot exclude an active connector port"
            )


def _connector_external_identity(
    connector: FiberConnectorPort,
) -> tuple[ODNEndpointType, uuid.UUID] | None:
    if connector.pon_port_id is not None:
        return ODNEndpointType.pon_port, connector.pon_port_id
    if connector.splitter_port_id is not None:
        return ODNEndpointType.splitter_port, connector.splitter_port_id
    if connector.ont_unit_id is not None:
        return ODNEndpointType.ont, connector.ont_unit_id
    return None


def _validate_connector(
    db: Session, connector: FiberConnectorPort, *, omit_id: uuid.UUID | None = None
) -> None:
    owners = (
        connector.patch_panel_id,
        connector.pon_port_id,
        connector.splitter_port_id,
        connector.ont_unit_id,
    )
    if sum(value is not None for value in owners) != 1:
        raise FiberPhysicalContinuityError(
            "fiber connector requires exactly one canonical owner"
        )
    if connector.connector_type not in CONNECTOR_TYPES:
        if connector.connector_type in {"mpo", "mtp"}:
            raise FiberPhysicalContinuityError(
                "MPO/MTP connector inventory requires an explicit assembly and lane model"
            )
        raise FiberPhysicalContinuityError("unsupported connector type")
    if connector.polish_type not in POLISH_TYPES:
        raise FiberPhysicalContinuityError("unsupported connector polish type")
    if connector.fiber_mode not in FIBER_MODES:
        raise FiberPhysicalContinuityError("unsupported connector fiber mode")
    if connector.patch_panel_id is not None:
        panel = db.get(FiberPatchPanel, connector.patch_panel_id)
        if panel is None or not panel.is_active:
            raise FiberPhysicalContinuityError(
                "active panel connector requires an active patch panel"
            )
        if connector.port_number is None or not (
            1 <= connector.port_number <= panel.port_capacity
        ):
            raise FiberPhysicalContinuityError(
                "panel connector port_number exceeds declared panel capacity"
            )
        if (
            connector.connector_type != panel.connector_type
            or connector.polish_type != panel.polish_type
            or connector.fiber_mode != panel.fiber_mode
        ):
            raise FiberPhysicalContinuityError(
                "panel connector type, polish, and fiber mode must match its panel"
            )
    else:
        if connector.port_number is not None:
            raise FiberPhysicalContinuityError(
                "equipment connector uses the canonical equipment port identity"
            )
        target: object | None
        if connector.pon_port_id is not None:
            target = db.get(PonPort, connector.pon_port_id)
        elif connector.splitter_port_id is not None:
            target = db.get(SplitterPort, connector.splitter_port_id)
        else:
            target = db.get(OntUnit, connector.ont_unit_id)
        if not _active(target):
            raise FiberPhysicalContinuityError(
                "active equipment connector requires an active canonical port"
            )
    if connector.is_active is False and connector.id is not None:
        if db.scalar(
            select(FiberStrandTermination.id).where(
                FiberStrandTermination.connector_port_id == connector.id,
                FiberStrandTermination.active.is_(True),
            )
        ):
            raise FiberPhysicalContinuityError(
                "retire the active strand termination before its connector"
            )
        if db.scalar(
            select(FiberPatchCord.id).where(
                FiberPatchCord.active.is_(True),
                or_(
                    FiberPatchCord.first_connector_port_id == connector.id,
                    FiberPatchCord.second_connector_port_id == connector.id,
                ),
            )
        ):
            raise FiberPhysicalContinuityError(
                "retire the active patch cord before its connector"
            )


def _guard_inventory_retirement(db: Session, asset: object) -> None:
    if isinstance(asset, FiberRack):
        if db.scalar(
            select(FiberPatchPanel.id).where(
                FiberPatchPanel.rack_id == asset.id,
                FiberPatchPanel.is_active.is_(True),
            )
        ):
            raise FiberPhysicalContinuityError(
                "retire active patch panels before retiring their rack"
            )
    elif isinstance(asset, FiberPatchPanel):
        if db.scalar(
            select(FiberConnectorPort.id).where(
                FiberConnectorPort.patch_panel_id == asset.id,
                FiberConnectorPort.is_active.is_(True),
            )
        ):
            raise FiberPhysicalContinuityError(
                "retire active connector ports before retiring their patch panel"
            )
    elif isinstance(asset, FiberConnectorPort):
        asset.is_active = False
        _validate_connector(db, asset, omit_id=asset.id)
        asset.is_active = True


INVENTORY_MODELS: dict[str, type] = {
    "fiber_rack": FiberRack,
    "fiber_patch_panel": FiberPatchPanel,
    "fiber_connector_port": FiberConnectorPort,
}


def apply_reviewed_physical_inventory_change(
    db: Session,
    *,
    asset_type: str,
    operation: FiberChangeRequestOperation,
    asset_id: object | None,
    payload: dict,
) -> FiberRack | FiberPatchPanel | FiberConnectorPort:
    """Apply reviewed rack/panel/connector inventory through one writer."""

    model = INVENTORY_MODELS.get(asset_type)
    if model is None:
        raise FiberPhysicalContinuityError("unsupported physical inventory type")
    prepared = dict(payload or {})
    for key, value in tuple(prepared.items()):
        if key.endswith("_id") and value not in (None, ""):
            prepared[key] = _uuid(value, key)
    validator = (
        _validate_rack
        if model is FiberRack
        else _validate_panel
        if model is FiberPatchPanel
        else _validate_connector
    )
    if operation == FiberChangeRequestOperation.create:
        asset = model(**prepared)
        if asset.id is None:
            asset.id = uuid.uuid4()
        validator(db, asset)
        db.add(asset)
        db.flush()
        return asset
    if asset_id is None:
        raise FiberPhysicalContinuityError(f"{operation.value} requires asset_id")
    asset = db.get(model, _uuid(asset_id, "asset_id"))
    if asset is None:
        raise FiberPhysicalContinuityError("physical inventory asset not found")
    if operation == FiberChangeRequestOperation.delete:
        _guard_inventory_retirement(db, asset)
        asset.is_active = False
        db.flush()
        return asset
    if operation != FiberChangeRequestOperation.update:
        raise FiberPhysicalContinuityError("unsupported physical inventory operation")
    before = {key: getattr(asset, key) for key in prepared if hasattr(asset, key)}
    try:
        for key, value in prepared.items():
            if key not in {"id", "created_at", "updated_at"} and hasattr(asset, key):
                setattr(asset, key, value)
        validator(db, asset, omit_id=asset.id)
        db.flush()
    except Exception:
        for key, value in before.items():
            setattr(asset, key, value)
        raise
    return asset


def _load_strand_end(
    db: Session, strand_id: uuid.UUID, strand_end: str, *, lock: bool
) -> tuple[FiberStrand, FiberSegment, FiberTerminationPoint]:
    if strand_end not in STRAND_ENDS:
        raise FiberPhysicalContinuityError("strand_end must be a or b")
    statement = select(FiberStrand).where(FiberStrand.id == strand_id)
    if lock:
        statement = statement.with_for_update()
    strand = db.scalar(statement)
    if strand is None or not strand.is_active or strand.segment_id is None:
        raise FiberPhysicalContinuityError(
            "physical link requires an active exact segment strand"
        )
    if strand.status in {FiberStrandStatus.damaged, FiberStrandStatus.retired}:
        raise FiberPhysicalContinuityError(
            "damaged or retired strand cannot carry physical continuity"
        )
    segment = db.get(FiberSegment, strand.segment_id)
    if segment is None or not segment.is_active:
        raise FiberPhysicalContinuityError(
            "physical link requires an active canonical cable segment"
        )
    point_id = segment.from_point_id if strand_end == "a" else segment.to_point_id
    point = db.get(FiberTerminationPoint, point_id) if point_id else None
    if point is None or not point.is_active or point.ref_id is None:
        raise FiberPhysicalContinuityError(
            "strand end requires an active exact segment termination"
        )
    return strand, segment, point


def _strand_end_occupied(
    db: Session, strand_id: uuid.UUID, strand_end: str, *, omit_link: uuid.UUID | None
) -> bool:
    splice_statement = select(FiberCoreSplice.id).where(
        FiberCoreSplice.active.is_(True),
        or_(
            and_(
                FiberCoreSplice.first_strand_id == strand_id,
                FiberCoreSplice.first_strand_end == strand_end,
            ),
            and_(
                FiberCoreSplice.second_strand_id == strand_id,
                FiberCoreSplice.second_strand_end == strand_end,
            ),
        ),
    )
    termination_statement = select(FiberStrandTermination.id).where(
        FiberStrandTermination.active.is_(True),
        FiberStrandTermination.strand_id == strand_id,
        FiberStrandTermination.strand_end == strand_end,
    )
    if omit_link is not None:
        splice_statement = splice_statement.where(FiberCoreSplice.id != omit_link)
        termination_statement = termination_statement.where(
            FiberStrandTermination.id != omit_link
        )
    return bool(
        db.scalar(splice_statement) is not None
        or db.scalar(termination_statement) is not None
    )


def _patch_connector_occupied(
    db: Session, connector_id: uuid.UUID, *, omit_link: uuid.UUID | None
) -> bool:
    statement = select(FiberPatchCord.id).where(
        FiberPatchCord.active.is_(True),
        or_(
            FiberPatchCord.first_connector_port_id == connector_id,
            FiberPatchCord.second_connector_port_id == connector_id,
        ),
    )
    if omit_link is not None:
        statement = statement.where(FiberPatchCord.id != omit_link)
    return db.scalar(statement) is not None


def _patch_component_has_in_use_strand(db: Session, patch: FiberPatchCord) -> bool:
    """Return whether either side of a patch belongs to an in-use core component."""

    node_adjacency: dict[tuple[str, uuid.UUID], set[tuple[str, uuid.UUID]]] = {}

    def connect(first: tuple[str, uuid.UUID], second: tuple[str, uuid.UUID]) -> None:
        node_adjacency.setdefault(first, set()).add(second)
        node_adjacency.setdefault(second, set()).add(first)

    for termination in db.scalars(
        select(FiberStrandTermination).where(FiberStrandTermination.active.is_(True))
    ).all():
        connect(
            ("strand", termination.strand_id),
            ("connector", termination.connector_port_id),
        )
    for splice in db.scalars(
        select(FiberCoreSplice).where(FiberCoreSplice.active.is_(True))
    ).all():
        connect(
            ("strand", splice.first_strand_id),
            ("strand", splice.second_strand_id),
        )
    for active_patch in db.scalars(
        select(FiberPatchCord).where(FiberPatchCord.active.is_(True))
    ).all():
        connect(
            ("connector", active_patch.first_connector_port_id),
            ("connector", active_patch.second_connector_port_id),
        )

    queue = deque(
        [
            ("connector", patch.first_connector_port_id),
            ("connector", patch.second_connector_port_id),
        ]
    )
    visited: set[tuple[str, uuid.UUID]] = set(queue)
    while queue:
        current = queue.popleft()
        for neighbor in node_adjacency.get(current, ()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    strand_ids = [node_id for node_type, node_id in visited if node_type == "strand"]
    if not strand_ids:
        return False
    return (
        db.scalar(
            select(FiberStrand.id).where(
                FiberStrand.id.in_(strand_ids),
                FiberStrand.status == FiberStrandStatus.in_use,
            )
        )
        is not None
    )


def _connector(
    db: Session, connector_id: uuid.UUID, *, lock: bool
) -> FiberConnectorPort:
    statement = select(FiberConnectorPort).where(FiberConnectorPort.id == connector_id)
    if lock:
        statement = statement.with_for_update()
    connector = db.scalar(statement)
    if connector is None or not connector.is_active:
        raise FiberPhysicalContinuityError("connector port is missing or inactive")
    _validate_connector(db, connector, omit_id=connector.id)
    return connector


def _rack_matches_point(
    db: Session, rack: FiberRack, point: FiberTerminationPoint
) -> bool:
    if (
        point.endpoint_type == ODNEndpointType.fdh
        and rack.fdh_cabinet_id == point.ref_id
    ):
        return True
    if (
        point.endpoint_type == ODNEndpointType.fiber_access_point
        and rack.fiber_access_point_id == point.ref_id
    ):
        return True
    if (
        point.endpoint_type == ODNEndpointType.splice_closure
        and rack.splice_closure_id == point.ref_id
    ):
        return True
    if point.endpoint_type == ODNEndpointType.splitter_port and rack.fdh_cabinet_id:
        port = db.get(SplitterPort, point.ref_id)
        splitter = db.get(Splitter, port.splitter_id) if port else None
        return bool(splitter and splitter.fdh_id == rack.fdh_cabinet_id)
    if point.endpoint_type == ODNEndpointType.pon_port and rack.pop_site_id:
        pon = db.get(PonPort, point.ref_id)
        if pon is None:
            return False
        nodes = list(
            db.scalars(
                select(NetworkDevice).where(
                    NetworkDevice.is_active.is_(True),
                    NetworkDevice.matched_device_type == "olt",
                    NetworkDevice.matched_device_id == pon.olt_id,
                    NetworkDevice.pop_site_id == rack.pop_site_id,
                )
            ).all()
        )
        return len(nodes) == 1
    return False


def _assert_termination_compatible(
    db: Session,
    point: FiberTerminationPoint,
    connector: FiberConnectorPort,
) -> None:
    external = _connector_external_identity(connector)
    if external is not None:
        if external != (point.endpoint_type, point.ref_id):
            raise FiberPhysicalContinuityError(
                "equipment connector disagrees with the exact segment endpoint"
            )
        return
    panel = db.get(FiberPatchPanel, connector.patch_panel_id)
    rack = db.get(FiberRack, panel.rack_id) if panel else None
    if panel is None or rack is None or not panel.is_active or not rack.is_active:
        raise FiberPhysicalContinuityError(
            "panel connector does not resolve through an active rack"
        )
    if not _rack_matches_point(db, rack, point):
        raise FiberPhysicalContinuityError(
            "rack host disagrees with the exact segment endpoint infrastructure"
        )


def _normalize_link_request(link_type: object, action: object) -> tuple[str, str]:
    normalized_type = str(link_type or "").strip().lower()
    normalized_action = str(action or "").strip().lower()
    if normalized_type not in LINK_TYPES:
        raise FiberPhysicalContinuityError("unsupported physical link_type")
    if normalized_action not in ACTIONS:
        raise FiberPhysicalContinuityError("action must be connect or disconnect")
    return normalized_type, normalized_action


def _disconnect_preview(
    db: Session, link_type: str, target_link_id: uuid.UUID, *, lock: bool
) -> FiberPhysicalLinkPreview:
    if link_type == "core_splice":
        splice_statement = select(FiberCoreSplice).where(
            FiberCoreSplice.id == target_link_id
        )
        if lock:
            splice_statement = splice_statement.with_for_update()
        splice = db.scalar(splice_statement)
        if splice is None or not splice.active:
            raise FiberPhysicalContinuityError("physical link is missing or inactive")
        strands = (
            db.get(FiberStrand, splice.first_strand_id),
            db.get(FiberStrand, splice.second_strand_id),
        )
        if any(
            strand is not None and strand.status == FiberStrandStatus.in_use
            for strand in strands
        ):
            raise FiberPhysicalContinuityError(
                "disconnect would break an in-use core; remove service use first"
            )
        return FiberPhysicalLinkPreview(
            link_type=link_type,
            action="disconnect",
            target_link_id=splice.id,
            first_strand_id=splice.first_strand_id,
            first_strand_end=splice.first_strand_end,
            second_strand_id=splice.second_strand_id,
            second_strand_end=splice.second_strand_end,
            splice_closure_id=splice.splice_closure_id,
            splice_tray_id=splice.splice_tray_id,
            position=splice.position,
            splice_type=splice.splice_type,
            insertion_loss_db=splice.insertion_loss_db,
        )
    if link_type == "strand_termination":
        termination_statement = select(FiberStrandTermination).where(
            FiberStrandTermination.id == target_link_id
        )
        if lock:
            termination_statement = termination_statement.with_for_update()
        termination = db.scalar(termination_statement)
        if termination is None or not termination.active:
            raise FiberPhysicalContinuityError("physical link is missing or inactive")
        strand = db.get(FiberStrand, termination.strand_id)
        if strand and strand.status == FiberStrandStatus.in_use:
            raise FiberPhysicalContinuityError(
                "disconnect would break an in-use core; remove service use first"
            )
        return FiberPhysicalLinkPreview(
            link_type=link_type,
            action="disconnect",
            target_link_id=termination.id,
            first_strand_id=termination.strand_id,
            first_strand_end=termination.strand_end,
            connector_port_id=termination.connector_port_id,
        )
    patch_statement = select(FiberPatchCord).where(FiberPatchCord.id == target_link_id)
    if lock:
        patch_statement = patch_statement.with_for_update()
    patch = db.scalar(patch_statement)
    if patch is None or not patch.active:
        raise FiberPhysicalContinuityError("physical link is missing or inactive")
    if _patch_component_has_in_use_strand(db, patch):
        raise FiberPhysicalContinuityError(
            "disconnect would break an in-use core; remove service use first"
        )
    return FiberPhysicalLinkPreview(
        link_type=link_type,
        action="disconnect",
        target_link_id=patch.id,
        first_connector_port_id=patch.first_connector_port_id,
        second_connector_port_id=patch.second_connector_port_id,
        label=patch.label,
        assembly_label=patch.assembly_label,
        length_m=patch.length_m,
        insertion_loss_db=patch.insertion_loss_db,
    )


def preview_physical_link(
    db: Session,
    link_type: str,
    action: str,
    *,
    target_link_id: object | None = None,
    first_strand_id: object | None = None,
    first_strand_end: str | None = None,
    second_strand_id: object | None = None,
    second_strand_end: str | None = None,
    connector_port_id: object | None = None,
    first_connector_port_id: object | None = None,
    second_connector_port_id: object | None = None,
    splice_closure_id: object | None = None,
    splice_tray_id: object | None = None,
    position: int | None = None,
    splice_type: str | None = None,
    label: str | None = None,
    assembly_label: str | None = None,
    length_m: object | None = None,
    insertion_loss_db: object | None = None,
    for_update: bool = False,
) -> FiberPhysicalLinkPreview:
    """Validate exact physical-link state and return a write-free preview."""

    normalized_type, normalized_action = _normalize_link_request(link_type, action)
    target_uuid = _optional_uuid(target_link_id, "target_link_id")
    if normalized_action == "disconnect":
        if target_uuid is None:
            raise FiberPhysicalContinuityError("disconnect requires target_link_id")
        return _disconnect_preview(db, normalized_type, target_uuid, lock=for_update)
    if target_uuid is not None:
        raise FiberPhysicalContinuityError("connect cannot name target_link_id")

    if normalized_type == "core_splice":
        first_id = _uuid(first_strand_id, "first_strand_id")
        second_id = _uuid(second_strand_id, "second_strand_id")
        first_end = str(first_strand_end or "").lower()
        second_end = str(second_strand_end or "").lower()
        first_key = (str(first_id), first_end)
        second_key = (str(second_id), second_end)
        if second_key < first_key:
            first_id, second_id = second_id, first_id
            first_end, second_end = second_end, first_end
        if first_id == second_id:
            raise FiberPhysicalContinuityError("core splice requires distinct strands")
        _first, _first_segment, first_point = _load_strand_end(
            db, first_id, first_end, lock=for_update
        )
        _second, _second_segment, second_point = _load_strand_end(
            db, second_id, second_end, lock=for_update
        )
        closure_id = _uuid(splice_closure_id, "splice_closure_id")
        closure = db.get(FiberSpliceClosure, closure_id)
        if closure is None or not closure.is_active:
            raise FiberPhysicalContinuityError("splice closure is missing or inactive")
        if any(
            point.endpoint_type != ODNEndpointType.splice_closure
            or point.ref_id != closure.id
            for point in (first_point, second_point)
        ):
            raise FiberPhysicalContinuityError(
                "both strand ends must terminate at the exact splice closure"
            )
        tray_id = _optional_uuid(splice_tray_id, "splice_tray_id")
        if tray_id is not None:
            tray = db.get(FiberSpliceTray, tray_id)
            if tray is None or tray.closure_id != closure.id:
                raise FiberPhysicalContinuityError(
                    "splice tray does not belong to the exact closure"
                )
        if position is not None and position <= 0:
            raise FiberPhysicalContinuityError("splice position must be positive")
        if _strand_end_occupied(
            db, first_id, first_end, omit_link=None
        ) or _strand_end_occupied(db, second_id, second_end, omit_link=None):
            raise FiberPhysicalContinuityError(
                "one of the exact strand ends already has an active continuity link"
            )
        return FiberPhysicalLinkPreview(
            link_type=normalized_type,
            action=normalized_action,
            first_strand_id=first_id,
            first_strand_end=first_end,
            second_strand_id=second_id,
            second_strand_end=second_end,
            splice_closure_id=closure.id,
            splice_tray_id=tray_id,
            position=position,
            splice_type=_required_text(splice_type, "splice_type", limit=80),
            insertion_loss_db=_decimal(insertion_loss_db, "insertion_loss_db"),
        )

    if normalized_type == "strand_termination":
        strand_id = _uuid(first_strand_id, "first_strand_id")
        strand_end = str(first_strand_end or "").lower()
        _strand, _segment, point = _load_strand_end(
            db, strand_id, strand_end, lock=for_update
        )
        connector_id = _uuid(connector_port_id, "connector_port_id")
        connector = _connector(db, connector_id, lock=for_update)
        _assert_termination_compatible(db, point, connector)
        if _strand_end_occupied(db, strand_id, strand_end, omit_link=None):
            raise FiberPhysicalContinuityError(
                "exact strand end already has an active continuity link"
            )
        if db.scalar(
            select(FiberStrandTermination.id).where(
                FiberStrandTermination.connector_port_id == connector.id,
                FiberStrandTermination.active.is_(True),
            )
        ):
            raise FiberPhysicalContinuityError(
                "connector already has an active back-side strand termination"
            )
        return FiberPhysicalLinkPreview(
            link_type=normalized_type,
            action=normalized_action,
            first_strand_id=strand_id,
            first_strand_end=strand_end,
            connector_port_id=connector.id,
        )

    first_connector_id = _uuid(first_connector_port_id, "first_connector_port_id")
    second_connector_id = _uuid(second_connector_port_id, "second_connector_port_id")
    if first_connector_id == second_connector_id:
        raise FiberPhysicalContinuityError("patch cord requires distinct connectors")
    if str(second_connector_id) < str(first_connector_id):
        first_connector_id, second_connector_id = (
            second_connector_id,
            first_connector_id,
        )
    first_connector = _connector(db, first_connector_id, lock=for_update)
    second_connector = _connector(db, second_connector_id, lock=for_update)
    if first_connector.fiber_mode != second_connector.fiber_mode:
        raise FiberPhysicalContinuityError(
            "patch cord cannot join different fiber modes"
        )
    if _patch_connector_occupied(
        db, first_connector.id, omit_link=None
    ) or _patch_connector_occupied(db, second_connector.id, omit_link=None):
        raise FiberPhysicalContinuityError(
            "one of the exact connectors already has an active patch cord"
        )
    return FiberPhysicalLinkPreview(
        link_type=normalized_type,
        action=normalized_action,
        first_connector_port_id=first_connector.id,
        second_connector_port_id=second_connector.id,
        label=_required_text(label, "label", limit=160),
        assembly_label=_optional_text(assembly_label, limit=160),
        length_m=_decimal(length_m, "length_m", positive=True),
        insertion_loss_db=_decimal(insertion_loss_db, "insertion_loss_db"),
    )


def _decision_payload(
    preview: FiberPhysicalLinkPreview, *, proposed_by: str, reason: str
) -> dict[str, object]:
    return {**preview.to_dict(), "proposed_by": proposed_by, "reason": reason}


def propose_physical_link(
    db: Session,
    link_type: str,
    action: str,
    *,
    proposed_by: str,
    reason: str,
    **values: object,
) -> FiberPhysicalLinkDecision:
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    preview = preview_physical_link(db, link_type, action, **values)  # type: ignore[arg-type]
    decision_sha256 = _digest(
        _decision_payload(preview, proposed_by=actor, reason=normalized_reason)
    )
    existing = db.scalar(
        select(FiberPhysicalLinkDecision).where(
            FiberPhysicalLinkDecision.decision_sha256 == decision_sha256
        )
    )
    if existing is not None:
        if existing.status in ACTIVE_DECISION_STATUSES:
            return existing
        raise FiberPhysicalContinuityError(
            "this exact physical-link decision is already terminal"
        )
    decision = FiberPhysicalLinkDecision(
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
        raise FiberPhysicalContinuityError(
            "canonical physical-link decision uniqueness conflict"
        ) from exc
    db.refresh(decision)
    return decision


def _decision(
    db: Session, decision_id: object, *, lock: bool
) -> FiberPhysicalLinkDecision:
    statement = select(FiberPhysicalLinkDecision).where(
        FiberPhysicalLinkDecision.id == _uuid(decision_id, "decision_id")
    )
    if lock:
        statement = statement.with_for_update()
    row = db.scalar(statement)
    if row is None:
        raise FiberPhysicalContinuityError("physical-link decision not found")
    return row


def _revalidate(
    db: Session, decision: FiberPhysicalLinkDecision, *, for_update: bool
) -> FiberPhysicalLinkPreview:
    preview = preview_physical_link(
        db,
        decision.link_type,
        decision.action,
        target_link_id=decision.target_link_id,
        first_strand_id=decision.first_strand_id,
        first_strand_end=decision.first_strand_end,
        second_strand_id=decision.second_strand_id,
        second_strand_end=decision.second_strand_end,
        connector_port_id=decision.connector_port_id,
        first_connector_port_id=decision.first_connector_port_id,
        second_connector_port_id=decision.second_connector_port_id,
        splice_closure_id=decision.splice_closure_id,
        splice_tray_id=decision.splice_tray_id,
        position=decision.position,
        splice_type=decision.splice_type,
        label=decision.label,
        assembly_label=decision.assembly_label,
        length_m=decision.length_m,
        insertion_loss_db=decision.insertion_loss_db,
        for_update=for_update,
    )
    for field, value in asdict(preview).items():
        if getattr(decision, field) != value:
            raise FiberPhysicalContinuityError(
                f"authoritative {field} changed after proposal"
            )
    return preview


def approve_physical_link(
    db: Session,
    decision_id: object,
    *,
    reviewed_by: str,
    review_notes: str,
    commit: bool = True,
) -> FiberPhysicalLinkDecision:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    row = _decision(db, decision_id, lock=True)
    if row.status != "proposed":
        if row.status == "approved" and row.reviewed_by == actor:
            return row
        raise FiberPhysicalContinuityError("physical-link decision is not proposed")
    if row.proposed_by == actor:
        raise FiberPhysicalContinuityError(
            "the proposer cannot review the same physical-link decision"
        )
    _revalidate(db, row, for_update=True)
    row.status = "approved"
    row.reviewed_by = actor
    row.review_notes = notes
    row.reviewed_at = datetime.now(UTC)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def decline_physical_link(
    db: Session,
    decision_id: object,
    *,
    reviewed_by: str,
    review_notes: str,
    commit: bool = True,
) -> FiberPhysicalLinkDecision:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    row = _decision(db, decision_id, lock=True)
    if row.status != "proposed":
        if row.status == "declined" and row.reviewed_by == actor:
            return row
        raise FiberPhysicalContinuityError("physical-link decision is not proposed")
    if row.proposed_by == actor:
        raise FiberPhysicalContinuityError(
            "the proposer cannot review the same physical-link decision"
        )
    row.status = "declined"
    row.reviewed_by = actor
    row.review_notes = notes
    row.reviewed_at = datetime.now(UTC)
    row.closed_reason = "physical_link_decision_declined"
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def _result(
    row: FiberPhysicalLinkDecision,
    *,
    actor: str,
    outcome: str,
    link_id: uuid.UUID | None,
) -> dict[str, object | None]:
    return {
        "action": row.action,
        "decision_id": str(row.id),
        "executed_by": actor,
        "link_id": str(link_id) if link_id else None,
        "link_type": row.link_type,
        "outcome": outcome,
        "schema_version": 1,
    }


def _set_result(
    row: FiberPhysicalLinkDecision,
    *,
    actor: str,
    status: str,
    payload: dict,
    closed_reason: str | None = None,
) -> None:
    row.status = status
    row.executed_by = actor
    row.executed_at = datetime.now(UTC)
    row.closed_reason = closed_reason
    row.result_payload = payload
    row.result_sha256 = _digest(payload)


def _apply_link(
    db: Session, row: FiberPhysicalLinkDecision
) -> FiberCoreSplice | FiberStrandTermination | FiberPatchCord:
    if row.action == "disconnect":
        if row.link_type == "core_splice":
            existing: (
                FiberCoreSplice | FiberStrandTermination | FiberPatchCord | None
            ) = db.get(FiberCoreSplice, row.target_link_id)
        elif row.link_type == "strand_termination":
            existing = db.get(FiberStrandTermination, row.target_link_id)
        else:
            existing = db.get(FiberPatchCord, row.target_link_id)
        if existing is None or not existing.active:
            raise FiberPhysicalContinuityError(
                "approved physical link changed before execution"
            )
        existing.active = False
        existing.retired_by_decision_id = row.id
        db.flush()
        return existing
    link: FiberCoreSplice | FiberStrandTermination | FiberPatchCord
    if row.link_type == "core_splice":
        link = FiberCoreSplice(
            first_strand_id=row.first_strand_id,
            first_strand_end=row.first_strand_end,
            second_strand_id=row.second_strand_id,
            second_strand_end=row.second_strand_end,
            splice_closure_id=row.splice_closure_id,
            splice_tray_id=row.splice_tray_id,
            position=row.position,
            splice_type=row.splice_type,
            insertion_loss_db=row.insertion_loss_db,
            created_by_decision_id=row.id,
            active=True,
            notes=row.reason,
        )
    elif row.link_type == "strand_termination":
        link = FiberStrandTermination(
            strand_id=row.first_strand_id,
            strand_end=row.first_strand_end,
            connector_port_id=row.connector_port_id,
            created_by_decision_id=row.id,
            active=True,
            notes=row.reason,
        )
    else:
        link = FiberPatchCord(
            first_connector_port_id=row.first_connector_port_id,
            second_connector_port_id=row.second_connector_port_id,
            label=row.label,
            assembly_label=row.assembly_label,
            length_m=row.length_m,
            insertion_loss_db=row.insertion_loss_db,
            created_by_decision_id=row.id,
            active=True,
            notes=row.reason,
        )
    db.add(link)
    db.flush()
    return link


def execute_physical_link(
    db: Session,
    decision_id: object,
    *,
    executed_by: str,
    commit: bool = True,
) -> FiberPhysicalLinkDecision:
    actor = _required_text(executed_by, "executed_by", limit=160)
    row = _decision(db, decision_id, lock=True)
    if row.status in {"applied", "closed"}:
        return row
    if row.status != "approved":
        raise FiberPhysicalContinuityError("physical-link decision is not approved")
    try:
        with db.begin_nested():
            _revalidate(db, row, for_update=True)
            link = _apply_link(db, row)
    except (FiberPhysicalContinuityError, IntegrityError) as exc:
        row = _decision(db, decision_id, lock=True)
        payload = _result(row, actor=actor, outcome="closed_stale", link_id=None)
        payload["reason"] = str(exc)
        _set_result(
            row,
            actor=actor,
            status="closed",
            payload=payload,
            closed_reason="authoritative_physical_inputs_changed",
        )
        if commit:
            db.commit()
            db.refresh(row)
        else:
            db.flush()
        return row
    payload = _result(row, actor=actor, outcome="applied", link_id=link.id)
    _set_result(row, actor=actor, status="applied", payload=payload)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


@dataclass(frozen=True)
class _GraphEdge:
    kind: str
    asset_id: uuid.UUID


Node = tuple[str, uuid.UUID]


def _add_edge(
    adjacency: dict[Node, list[tuple[Node, _GraphEdge]]],
    first: Node,
    second: Node,
    edge: _GraphEdge,
) -> None:
    adjacency.setdefault(first, []).append((second, edge))
    adjacency.setdefault(second, []).append((first, edge))


EdgeKey = tuple[str, uuid.UUID]


def _edge_key(edge: _GraphEdge) -> EdgeKey:
    return edge.kind, edge.asset_id


def _path_has_alternate_route(
    adjacency: dict[Node, list[tuple[Node, _GraphEdge]]],
    start: Node,
    path_edges: list[_GraphEdge],
) -> bool:
    """Return whether any selected path edge is not a graph bridge.

    An undirected start-to-end path is unique only when every edge on that path
    is a bridge. Counting shortest paths is insufficient because a physical
    cycle can provide a longer alternate optical route.
    """

    discovery: dict[Node, int] = {start: 0}
    low: dict[Node, int] = {start: 0}
    parent: dict[Node, Node] = {}
    parent_edge: dict[Node, EdgeKey] = {}
    iterators: dict[Node, Iterator[tuple[Node, _GraphEdge]]] = {
        start: iter(adjacency.get(start, ()))
    }
    bridges: set[EdgeKey] = set()
    stack = [start]
    next_discovery = 1

    while stack:
        node = stack[-1]
        try:
            neighbor, edge = next(iterators[node])
        except StopIteration:
            stack.pop()
            parent_node = parent.get(node)
            if parent_node is not None:
                low[parent_node] = min(low[parent_node], low[node])
                if low[node] > discovery[parent_node]:
                    bridges.add(parent_edge[node])
            continue

        key = _edge_key(edge)
        if parent_edge.get(node) == key:
            continue
        if neighbor not in discovery:
            parent[neighbor] = node
            parent_edge[neighbor] = key
            discovery[neighbor] = next_discovery
            low[neighbor] = next_discovery
            next_discovery += 1
            iterators[neighbor] = iter(adjacency.get(neighbor, ()))
            stack.append(neighbor)
            continue
        low[node] = min(low[node], discovery[neighbor])

    return any(_edge_key(edge) not in bridges for edge in path_edges)


def _external_connector(
    db: Session, endpoint_type: str, endpoint_id: uuid.UUID
) -> tuple[FiberConnectorPort | None, str | None]:
    column = {
        "pon_port": FiberConnectorPort.pon_port_id,
        "splitter_port": FiberConnectorPort.splitter_port_id,
        "ont": FiberConnectorPort.ont_unit_id,
    }.get(endpoint_type)
    if column is None:
        return None, "unsupported_external_endpoint"
    rows = list(
        db.scalars(
            select(FiberConnectorPort).where(
                column == endpoint_id,
                FiberConnectorPort.is_active.is_(True),
            )
        ).all()
    )
    if not rows:
        return None, "external_connector_missing"
    if len(rows) != 1:
        return None, "external_connector_conflict"
    return rows[0], None


def _connector_label(connector: FiberConnectorPort) -> str:
    if connector.patch_panel_id is not None:
        return connector.label or f"Panel port {connector.port_number}"
    if connector.pon_port_id is not None:
        return connector.label or "PON optical connector"
    if connector.splitter_port_id is not None:
        return connector.label or "Splitter optical connector"
    return connector.label or "ONT optical connector"


def _connector_hops(
    db: Session, connector: FiberConnectorPort
) -> list[FiberCoreContinuityHop]:
    if connector.patch_panel_id is None:
        return [
            FiberCoreContinuityHop(
                kind="equipment_connector",
                asset_id=connector.id,
                label=_connector_label(connector),
                evidence_refs=(f"fiber-connector:{connector.id}",),
                metadata={
                    "connector_type": connector.connector_type,
                    "fiber_mode": connector.fiber_mode,
                    "polish_type": connector.polish_type,
                },
            )
        ]
    panel = db.get(FiberPatchPanel, connector.patch_panel_id)
    rack = db.get(FiberRack, panel.rack_id) if panel else None
    if panel is None or rack is None:
        return []
    return [
        FiberCoreContinuityHop(
            kind="fiber_rack",
            asset_id=rack.id,
            label=f"{rack.code} — {rack.name}",
            evidence_refs=(f"fiber-rack:{rack.id}",),
            metadata={"rack_units": rack.rack_units},
        ),
        FiberCoreContinuityHop(
            kind=panel.panel_type,
            asset_id=panel.id,
            label=panel.name,
            evidence_refs=(f"fiber-patch-panel:{panel.id}",),
            metadata={
                "port_capacity": panel.port_capacity,
                "rack_unit_height": panel.rack_unit_height,
                "rack_unit_start": panel.rack_unit_start,
            },
        ),
        FiberCoreContinuityHop(
            kind="patch_port",
            asset_id=connector.id,
            label=_connector_label(connector),
            evidence_refs=(f"fiber-connector:{connector.id}",),
            metadata={
                "connector_type": connector.connector_type,
                "fiber_mode": connector.fiber_mode,
                "polish_type": connector.polish_type,
                "port_number": connector.port_number,
            },
        ),
    ]


def resolve_core_continuity(
    db: Session,
    *,
    start_endpoint_type: str,
    start_endpoint_id: object,
    end_endpoint_type: str,
    end_endpoint_id: object,
    logical_segment_ids: tuple[object, ...] | list[object],
) -> FiberCoreContinuityResult:
    """Resolve one unique exact optical route across cores, splices, and patches."""

    segment_ids = tuple(
        _uuid(value, "logical_segment_id") for value in logical_segment_ids
    )
    base_payload = {
        "end_endpoint_id": str(end_endpoint_id),
        "end_endpoint_type": end_endpoint_type,
        "logical_segment_ids": [str(value) for value in segment_ids],
        "start_endpoint_id": str(start_endpoint_id),
        "start_endpoint_type": start_endpoint_type,
    }
    gaps: list[FiberCoreContinuityGap] = []
    if not segment_ids:
        gaps.append(
            FiberCoreContinuityGap(
                "core.logical_segments_missing",
                "Core continuity requires the exact ordered logical cable segments.",
            )
        )
        return FiberCoreContinuityResult(
            (), tuple(gaps), segment_ids, _digest(base_payload)
        )
    start, start_gap = _external_connector(
        db, start_endpoint_type, _uuid(start_endpoint_id, "start_endpoint_id")
    )
    end, end_gap = _external_connector(
        db, end_endpoint_type, _uuid(end_endpoint_id, "end_endpoint_id")
    )
    if start_gap:
        gaps.append(
            FiberCoreContinuityGap(
                f"core.start_{start_gap}",
                "The logical path start has no one exact active optical connector.",
            )
        )
    if end_gap:
        gaps.append(
            FiberCoreContinuityGap(
                f"core.end_{end_gap}",
                "The logical path end has no one exact active optical connector.",
            )
        )
    if gaps or start is None or end is None:
        return FiberCoreContinuityResult(
            (),
            tuple(gaps),
            segment_ids,
            _digest({**base_payload, "gaps": [asdict(g) for g in gaps]}),
        )

    strands = list(
        db.scalars(
            select(FiberStrand).where(
                FiberStrand.segment_id.in_(segment_ids),
                FiberStrand.is_active.is_(True),
            )
        ).all()
    )
    strand_by_id = {row.id: row for row in strands}
    allowed_strand_ids = set(strand_by_id)
    adjacency: dict[Node, list[tuple[Node, _GraphEdge]]] = {}
    connectors = list(
        db.scalars(
            select(FiberConnectorPort).where(FiberConnectorPort.is_active.is_(True))
        ).all()
    )
    connector_by_id = {row.id: row for row in connectors}
    for termination_row in db.scalars(
        select(FiberStrandTermination).where(
            FiberStrandTermination.active.is_(True),
            FiberStrandTermination.strand_id.in_(allowed_strand_ids),
        )
    ).all():
        _add_edge(
            adjacency,
            ("strand", termination_row.strand_id),
            ("connector", termination_row.connector_port_id),
            _GraphEdge("strand_termination", termination_row.id),
        )
    for splice_row in db.scalars(
        select(FiberCoreSplice).where(
            FiberCoreSplice.active.is_(True),
            FiberCoreSplice.first_strand_id.in_(allowed_strand_ids),
            FiberCoreSplice.second_strand_id.in_(allowed_strand_ids),
        )
    ).all():
        _add_edge(
            adjacency,
            ("strand", splice_row.first_strand_id),
            ("strand", splice_row.second_strand_id),
            _GraphEdge("core_splice", splice_row.id),
        )
    for patch_row in db.scalars(
        select(FiberPatchCord).where(FiberPatchCord.active.is_(True))
    ).all():
        _add_edge(
            adjacency,
            ("connector", patch_row.first_connector_port_id),
            ("connector", patch_row.second_connector_port_id),
            _GraphEdge("patch_cord", patch_row.id),
        )

    start_node: Node = ("connector", start.id)
    end_node: Node = ("connector", end.id)
    distance = {start_node: 0}
    predecessor: dict[Node, tuple[Node, _GraphEdge] | None] = {start_node: None}
    queue = deque([start_node])
    while queue:
        current = queue.popleft()
        if distance[current] >= 512:
            continue
        for neighbor, edge in adjacency.get(current, ()):
            candidate = distance[current] + 1
            if neighbor not in distance:
                distance[neighbor] = candidate
                predecessor[neighbor] = (current, edge)
                queue.append(neighbor)
    if end_node not in distance:
        gaps.append(
            FiberCoreContinuityGap(
                "core.continuity_missing",
                "No active exact strand/splice/termination/patch path connects the logical endpoints.",
                start.id,
            )
        )
        return FiberCoreContinuityResult(
            (),
            tuple(gaps),
            segment_ids,
            _digest({**base_payload, "gaps": [asdict(g) for g in gaps]}),
        )
    nodes: list[Node] = [end_node]
    edges: list[_GraphEdge] = []
    current = end_node
    while True:
        prior = predecessor[current]
        if prior is None:
            break
        previous, edge = prior
        edges.append(edge)
        nodes.append(previous)
        current = previous
    nodes.reverse()
    edges.reverse()
    if _path_has_alternate_route(adjacency, start_node, edges):
        gaps.append(
            FiberCoreContinuityGap(
                "core.continuity_ambiguous",
                "More than one exact physical core route requires review.",
                start.id,
            )
        )
        return FiberCoreContinuityResult(
            (),
            tuple(gaps),
            segment_ids,
            _digest({**base_payload, "gaps": [asdict(g) for g in gaps]}),
        )
    used_segments = tuple(
        strand_by_id[node_id].segment_id
        for node_type, node_id in nodes
        if node_type == "strand"
    )
    if used_segments != segment_ids:
        gaps.append(
            FiberCoreContinuityGap(
                "core.logical_segment_order_mismatch",
                "The exact core path does not traverse every logical cable segment once and in order.",
                start.id,
            )
        )

    hops: list[FiberCoreContinuityHop] = []
    for index, node in enumerate(nodes):
        node_type, node_id = node
        if node_type == "connector":
            connector = connector_by_id.get(node_id)
            if connector is not None:
                hops.extend(_connector_hops(db, connector))
        else:
            strand = strand_by_id[node_id]
            segment = db.get(FiberSegment, strand.segment_id)
            hops.append(
                FiberCoreContinuityHop(
                    kind="fiber_strand",
                    asset_id=strand.id,
                    label=(
                        f"{segment.name} core {strand.strand_number}"
                        if segment
                        else f"Core {strand.strand_number}"
                    ),
                    evidence_refs=(
                        f"fiber-strand:{strand.id}",
                        f"fiber-segment:{strand.segment_id}",
                    ),
                    metadata={
                        "segment_id": str(strand.segment_id),
                        "status": strand.status.value,
                        "strand_number": strand.strand_number,
                    },
                )
            )
            if strand.status != FiberStrandStatus.in_use:
                gaps.append(
                    FiberCoreContinuityGap(
                        "core.strand_not_in_use",
                        "An exact core on the active customer path is not marked in_use.",
                        strand.id,
                    )
                )
        if index < len(edges):
            edge = edges[index]
            metadata: dict[str, object] | None
            if edge.kind == "patch_cord":
                patch = db.get(FiberPatchCord, edge.asset_id)
                label = patch.label if patch else "Patch cord"
                metadata = (
                    {
                        "assembly_label": patch.assembly_label,
                        "insertion_loss_db": (
                            str(patch.insertion_loss_db)
                            if patch.insertion_loss_db is not None
                            else None
                        ),
                        "length_m": (
                            str(patch.length_m) if patch.length_m is not None else None
                        ),
                    }
                    if patch
                    else None
                )
            elif edge.kind == "core_splice":
                splice = db.get(FiberCoreSplice, edge.asset_id)
                label = "Reviewed core splice"
                metadata = (
                    {
                        "insertion_loss_db": (
                            str(splice.insertion_loss_db)
                            if splice and splice.insertion_loss_db is not None
                            else None
                        ),
                        "position": splice.position if splice else None,
                        "splice_type": splice.splice_type if splice else None,
                        "splice_closure_id": (
                            str(splice.splice_closure_id) if splice else None
                        ),
                        "splice_tray_id": (
                            str(splice.splice_tray_id)
                            if splice and splice.splice_tray_id
                            else None
                        ),
                    }
                    if splice
                    else None
                )
            else:
                label = "Reviewed strand termination"
                metadata = None
            hops.append(
                FiberCoreContinuityHop(
                    kind=edge.kind,
                    asset_id=edge.asset_id,
                    label=label,
                    evidence_refs=(f"physical-link:{edge.asset_id}",),
                    metadata=metadata,
                )
            )
    evidence_payload = {
        **base_payload,
        "gaps": [asdict(gap) for gap in gaps],
        "hops": [asdict(hop) for hop in hops],
    }
    return FiberCoreContinuityResult(
        tuple(hops), tuple(gaps), segment_ids, _digest(evidence_payload)
    )


def resolve_subscription_core_continuity(
    db: Session, subscription: object
) -> FiberCoreContinuityResult:
    """Compose every segment group in one customer trace at exact core level."""

    from app.services.fiber_topology import trace_fiber_subscription

    subscription_id = getattr(subscription, "id", subscription)
    trace = trace_fiber_subscription(db, subscription_id)
    if not trace.customer_trace_complete:
        gap = FiberCoreContinuityGap(
            "core.logical_path_incomplete",
            "Exact core continuity requires a complete canonical segment/splitter trace.",
        )
        return FiberCoreContinuityResult(
            (),
            (gap,),
            (),
            _digest({"gap": asdict(gap), "subscription_id": str(subscription_id)}),
        )
    boundaries = {"pon_port", "splitter_input", "splitter_output", "ont"}
    segment_kinds = {"feeder_segment", "distribution_segment", "drop_segment"}
    last_boundary: tuple[str, uuid.UUID] | None = None
    group_start: tuple[str, uuid.UUID] | None = None
    group_segments: list[uuid.UUID] = []
    groups: list[
        tuple[tuple[str, uuid.UUID], tuple[str, uuid.UUID], tuple[uuid.UUID, ...]]
    ] = []
    for hop in trace.hops:
        if hop.kind in boundaries and hop.asset_id is not None:
            boundary_type = (
                "splitter_port" if hop.kind.startswith("splitter_") else hop.kind
            )
            boundary = (boundary_type, _uuid(hop.asset_id, "boundary_asset_id"))
            if group_start is not None and group_segments:
                groups.append((group_start, boundary, tuple(group_segments)))
                group_start = None
                group_segments = []
            last_boundary = boundary
        elif hop.kind in segment_kinds and hop.asset_id is not None:
            if group_start is None:
                group_start = last_boundary
            group_segments.append(_uuid(hop.asset_id, "segment_id"))
    if group_start is not None or group_segments:
        gap = FiberCoreContinuityGap(
            "core.logical_boundary_missing",
            "A logical segment group has no exact optical endpoint boundary.",
        )
        return FiberCoreContinuityResult(
            (), (gap,), tuple(group_segments), _digest(asdict(gap))
        )
    all_hops: list[FiberCoreContinuityHop] = []
    all_gaps: list[FiberCoreContinuityGap] = []
    all_segments: list[uuid.UUID] = []
    report_hashes: list[str] = []
    for start, end, segment_ids in groups:
        result = resolve_core_continuity(
            db,
            start_endpoint_type=start[0],
            start_endpoint_id=start[1],
            end_endpoint_type=end[0],
            end_endpoint_id=end[1],
            logical_segment_ids=segment_ids,
        )
        all_hops.extend(result.hops)
        all_gaps.extend(result.gaps)
        all_segments.extend(result.logical_segment_ids)
        report_hashes.append(result.evidence_sha256)
    payload = {
        "gaps": [asdict(gap) for gap in all_gaps],
        "group_evidence_sha256": report_hashes,
        "hops": [asdict(hop) for hop in all_hops],
        "logical_segment_ids": [str(value) for value in all_segments],
        "subscription_id": str(subscription_id),
    }
    return FiberCoreContinuityResult(
        tuple(all_hops), tuple(all_gaps), tuple(all_segments), _digest(payload)
    )


def physical_inventory_http_error(exc: FiberPhysicalContinuityError) -> HTTPException:
    """Translate canonical physical-owner errors for generic change adapters."""

    return HTTPException(status_code=422, detail=str(exc))


__all__ = [
    "FiberCoreContinuityGap",
    "FiberCoreContinuityHop",
    "FiberCoreContinuityResult",
    "FiberPhysicalContinuityError",
    "FiberPhysicalLinkPreview",
    "apply_reviewed_physical_inventory_change",
    "approve_physical_link",
    "decline_physical_link",
    "execute_physical_link",
    "preview_physical_link",
    "propose_physical_link",
    "resolve_core_continuity",
    "resolve_subscription_core_continuity",
]
