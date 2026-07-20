"""Canonical support structures and reviewed exact asset-mount commands.

Imported pole rows remain observations. Canonical support identity is created or
changed only after the passive-asset change owner delegates an approved request
here. Mount edges use their own preview, independent review, locked execution,
and exact result evidence because identity does not imply attachment.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.fiber_change_request import FiberChangeRequestOperation
from app.models.fiber_support import (
    FiberSupportMount,
    FiberSupportMountDecision,
    FiberSupportStructure,
)
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
)
from app.services.audit_adapter import stage_audit_event
from app.services.common import coerce_uuid

MOUNTED_ASSET_MODELS: dict[str, Any] = {
    "fdh_cabinet": FdhCabinet,
    "fiber_access_point": FiberAccessPoint,
    "splice_closure": FiberSpliceClosure,
    "fiber_segment": FiberSegment,
}
SUPPORT_TYPES = frozenset({"pole", "tower", "building_attachment", "other"})
OWNERSHIP_STATUSES = frozenset({"unknown", "dotmac_owned", "leased", "third_party"})
LIFECYCLE_STATUSES = frozenset({"planned", "active", "suspended", "retired"})
INSPECTION_STATUSES = frozenset(
    {"uninspected", "due", "passed", "conditional", "failed"}
)
LEASE_STATUSES = frozenset(
    {"unknown", "not_required", "pending", "active", "expired", "terminated"}
)
MOUNT_ROLES = frozenset({"hosted", "route_support", "anchor"})
ACTIVE_DECISION_STATUSES = ("proposed", "approved")
_SHA256_HEX = frozenset("0123456789abcdef")


class FiberSupportStructureError(ValueError):
    """Raised when support identity, state, or exact mount evidence is invalid."""


@dataclass(frozen=True)
class FiberSupportMountPreview:
    action: str
    support_structure_id: uuid.UUID
    mounted_asset_type: str
    mounted_asset_id: uuid.UUID
    mount_role: str
    sequence: int | None
    existing_mount_id: uuid.UUID | None
    expected_support_state_sha256: str
    expected_asset_state_sha256: str
    expected_mount_state_sha256: str | None
    reason: str
    proposed_by: str
    decision_sha256: str
    existing_decision_id: uuid.UUID | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "decision_sha256": self.decision_sha256,
            "existing_decision_id": (
                str(self.existing_decision_id) if self.existing_decision_id else None
            ),
            "existing_mount_id": (
                str(self.existing_mount_id) if self.existing_mount_id else None
            ),
            "expected_asset_state_sha256": self.expected_asset_state_sha256,
            "expected_mount_state_sha256": self.expected_mount_state_sha256,
            "expected_support_state_sha256": self.expected_support_state_sha256,
            "mount_role": self.mount_role,
            "mounted_asset_id": str(self.mounted_asset_id),
            "mounted_asset_type": self.mounted_asset_type,
            "proposed_by": self.proposed_by,
            "reason": self.reason,
            "sequence": self.sequence,
            "support_structure_id": str(self.support_structure_id),
        }


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


def _sha256(value: object, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in _SHA256_HEX for char in normalized):
        raise FiberSupportStructureError(f"{field} must be a SHA-256 value")
    return normalized


def _text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberSupportStructureError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberSupportStructureError(f"{field} must be at most {limit} characters")
    return normalized


def _optional_text(value: object, field: str, *, limit: int) -> str | None:
    if value is None or not str(value).strip():
        return None
    return _text(value, field, limit=limit)


def _choice(value: object, field: str, choices: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in choices:
        raise FiberSupportStructureError(f"{field} is unsupported")
    return normalized


def _uuid(value: object, field: str) -> uuid.UUID:
    try:
        return coerce_uuid(value)
    except (TypeError, ValueError) as exc:
        raise FiberSupportStructureError(f"{field} must be a UUID") from exc


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _optional_datetime(value: object, field: str) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise FiberSupportStructureError(
                f"{field} must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FiberSupportStructureError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _optional_coordinate(
    value: object, field: str, *, minimum: float, maximum: float
) -> float | None:
    if value is None or not str(value).strip():
        return None
    if isinstance(value, bool):
        raise FiberSupportStructureError(f"{field} must be numeric")
    try:
        parsed = float(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberSupportStructureError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise FiberSupportStructureError(
            f"{field} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def _geojson_to_geom(geojson: dict) -> object:
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geojson)), 4326)


def _normalized_support_payload(payload: dict[str, Any], *, partial: bool) -> dict:
    allowed = {
        "code",
        "name",
        "support_type",
        "owner_name",
        "ownership_status",
        "lifecycle_status",
        "inspection_status",
        "last_inspected_at",
        "next_inspection_due_at",
        "lease_status",
        "lease_reference",
        "lease_starts_at",
        "lease_ends_at",
        "latitude",
        "longitude",
        "geojson",
        "notes",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise FiberSupportStructureError(
            "unsupported support fields: " + ", ".join(sorted(unknown))
        )
    data = dict(payload)
    if not partial or "code" in data:
        data["code"] = _text(data.get("code"), "code", limit=80)
    if not partial or "name" in data:
        data["name"] = _text(data.get("name"), "name", limit=160)
    choices = {
        "support_type": (SUPPORT_TYPES, "pole"),
        "ownership_status": (OWNERSHIP_STATUSES, "unknown"),
        "lifecycle_status": (LIFECYCLE_STATUSES, "active"),
        "inspection_status": (INSPECTION_STATUSES, "uninspected"),
        "lease_status": (LEASE_STATUSES, "unknown"),
    }
    for field, (values, default) in choices.items():
        if not partial or field in data:
            data[field] = _choice(data.get(field, default), field, values)
    for field, limit in (
        ("owner_name", 160),
        ("lease_reference", 160),
        ("notes", 4000),
    ):
        if field in data:
            data[field] = _optional_text(data[field], field, limit=limit)
    for field in (
        "last_inspected_at",
        "next_inspection_due_at",
        "lease_starts_at",
        "lease_ends_at",
    ):
        if field in data:
            data[field] = _optional_datetime(data[field], field)
    if "latitude" in data:
        data["latitude"] = _optional_coordinate(
            data["latitude"], "latitude", minimum=-90, maximum=90
        )
    if "longitude" in data:
        data["longitude"] = _optional_coordinate(
            data["longitude"], "longitude", minimum=-180, maximum=180
        )
    if (
        data.get("lease_starts_at") is not None
        and data.get("lease_ends_at") is not None
        and data["lease_ends_at"] <= data["lease_starts_at"]
    ):
        raise FiberSupportStructureError("lease_ends_at must be after lease_starts_at")
    if (
        data.get("last_inspected_at") is not None
        and data.get("next_inspection_due_at") is not None
        and data["next_inspection_due_at"] <= data["last_inspected_at"]
    ):
        raise FiberSupportStructureError(
            "next_inspection_due_at must be after last_inspected_at"
        )
    geojson = data.pop("geojson", None)
    if geojson is not None:
        if not isinstance(geojson, dict) or geojson.get("type") != "Point":
            raise FiberSupportStructureError("support geometry must be a GeoJSON Point")
        coordinates = geojson.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            raise FiberSupportStructureError(
                "support geometry must contain longitude and latitude"
            )
        longitude = _optional_coordinate(
            coordinates[0], "geojson longitude", minimum=-180, maximum=180
        )
        latitude = _optional_coordinate(
            coordinates[1], "geojson latitude", minimum=-90, maximum=90
        )
        assert longitude is not None and latitude is not None
        if data.get("longitude") not in {None, longitude}:
            raise FiberSupportStructureError(
                "longitude must match the exact GeoJSON point"
            )
        if data.get("latitude") not in {None, latitude}:
            raise FiberSupportStructureError(
                "latitude must match the exact GeoJSON point"
            )
        data["longitude"] = longitude
        data["latitude"] = latitude
        data["geom"] = _geojson_to_geom(geojson)
    elif "latitude" in data or "longitude" in data:
        raise FiberSupportStructureError(
            "coordinate changes require an exact GeoJSON point"
        )
    return data


def apply_reviewed_support_change(
    db: Session,
    *,
    operation: FiberChangeRequestOperation,
    asset_id: object | None,
    payload: dict[str, Any],
) -> FiberSupportStructure:
    """Apply a change already approved by the passive-asset change owner."""

    if operation == FiberChangeRequestOperation.create:
        if asset_id is not None:
            raise FiberSupportStructureError("support create cannot name asset_id")
        created = FiberSupportStructure(
            **_normalized_support_payload(payload, partial=False)
        )
        db.add(created)
        db.flush()
        return created
    support_id = _uuid(asset_id, "asset_id")
    row = db.scalar(
        select(FiberSupportStructure)
        .where(FiberSupportStructure.id == support_id)
        .with_for_update()
    )
    if row is None:
        raise FiberSupportStructureError("canonical support structure not found")
    if operation == FiberChangeRequestOperation.update:
        data = _normalized_support_payload(payload, partial=True)
        if data.get("lifecycle_status") == "retired" and _has_active_mounts(db, row.id):
            raise FiberSupportStructureError(
                "detach every active mount before retiring a support structure"
            )
        for field, value in data.items():
            setattr(row, field, value)
        if (
            row.lease_starts_at is not None
            and row.lease_ends_at is not None
            and row.lease_ends_at <= row.lease_starts_at
        ):
            raise FiberSupportStructureError(
                "lease_ends_at must be after lease_starts_at"
            )
        if (
            row.last_inspected_at is not None
            and row.next_inspection_due_at is not None
            and row.next_inspection_due_at <= row.last_inspected_at
        ):
            raise FiberSupportStructureError(
                "next_inspection_due_at must be after last_inspected_at"
            )
        db.flush()
        return row
    if operation == FiberChangeRequestOperation.delete:
        if _has_active_mounts(db, row.id):
            raise FiberSupportStructureError(
                "detach every active mount before retiring a support structure"
            )
        row.lifecycle_status = "retired"
        db.flush()
        return row
    raise FiberSupportStructureError("unsupported support change operation")


def _has_active_mounts(db: Session, support_id: uuid.UUID) -> bool:
    return (
        db.scalar(
            select(FiberSupportMount.id).where(
                FiberSupportMount.support_structure_id == support_id,
                FiberSupportMount.is_active.is_(True),
            )
        )
        is not None
    )


def _support_state(row: FiberSupportStructure) -> dict[str, object]:
    return {
        "code": row.code,
        "id": str(row.id),
        "inspection_status": row.inspection_status,
        "last_inspected_at": _timestamp(row.last_inspected_at),
        "latitude": row.latitude,
        "lease_ends_at": _timestamp(row.lease_ends_at),
        "lease_reference": row.lease_reference,
        "lease_starts_at": _timestamp(row.lease_starts_at),
        "lease_status": row.lease_status,
        "lifecycle_status": row.lifecycle_status,
        "longitude": row.longitude,
        "name": row.name,
        "next_inspection_due_at": _timestamp(row.next_inspection_due_at),
        "owner_name": row.owner_name,
        "ownership_status": row.ownership_status,
        "support_type": row.support_type,
        "updated_at": _timestamp(row.updated_at),
    }


def _asset_state(asset_type: str, row: Any) -> dict[str, object]:
    payload: dict[str, object] = {
        "asset_type": asset_type,
        "id": str(row.id),
        "is_active": bool(getattr(row, "is_active", True)),
        "updated_at": _timestamp(getattr(row, "updated_at", None)),
    }
    if asset_type == "fiber_segment":
        payload.update(
            {
                "from_point_id": str(row.from_point_id) if row.from_point_id else None,
                "to_point_id": str(row.to_point_id) if row.to_point_id else None,
            }
        )
    return payload


def _mount_state(row: FiberSupportMount) -> dict[str, object]:
    return {
        "id": str(row.id),
        "installed_at": _timestamp(row.installed_at),
        "installed_by": row.installed_by,
        "is_active": row.is_active,
        "mount_role": row.mount_role,
        "mounted_asset_id": str(row.mounted_asset_id),
        "mounted_asset_type": row.mounted_asset_type,
        "origin_decision_id": str(row.decision_id),
        "removed_at": _timestamp(row.removed_at),
        "removed_by": row.removed_by,
        "sequence": row.sequence,
        "support_structure_id": str(row.support_structure_id),
        "updated_at": _timestamp(row.updated_at),
    }


def _load_support(
    db: Session, support_id: uuid.UUID, *, lock: bool = False
) -> FiberSupportStructure:
    stmt = select(FiberSupportStructure).where(FiberSupportStructure.id == support_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None:
        raise FiberSupportStructureError("canonical support structure not found")
    if row.lifecycle_status != "active":
        raise FiberSupportStructureError("canonical support structure is not active")
    return row


def _load_asset(
    db: Session, asset_type: str, asset_id: uuid.UUID, *, lock: bool = False
) -> object:
    model = MOUNTED_ASSET_MODELS.get(asset_type)
    if model is None:
        raise FiberSupportStructureError("mounted_asset_type is unsupported")
    stmt = select(model).where(model.id == asset_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None:
        raise FiberSupportStructureError("canonical mounted asset not found")
    if getattr(row, "is_active", True) is False:
        raise FiberSupportStructureError("canonical mounted asset is inactive")
    return row


def _load_mount(
    db: Session, mount_id: uuid.UUID, *, lock: bool = False
) -> FiberSupportMount:
    stmt = select(FiberSupportMount).where(FiberSupportMount.id == mount_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None:
        raise FiberSupportStructureError("support mount not found")
    return row


def _normalize_mount_shape(
    *, asset_type: str, mount_role: str, sequence: int | None
) -> tuple[str, str, int | None]:
    normalized_type = str(asset_type or "").strip().lower()
    if normalized_type not in MOUNTED_ASSET_MODELS:
        raise FiberSupportStructureError("mounted_asset_type is unsupported")
    normalized_role = _choice(mount_role, "mount_role", MOUNT_ROLES)
    if normalized_type == "fiber_segment":
        if normalized_role not in {"route_support", "anchor"}:
            raise FiberSupportStructureError(
                "fiber segments require route_support or anchor role"
            )
        if sequence is None or sequence <= 0:
            raise FiberSupportStructureError(
                "fiber segment mounts require a positive sequence"
            )
        return normalized_type, normalized_role, sequence
    if normalized_role != "hosted":
        raise FiberSupportStructureError("point assets require the hosted mount role")
    if sequence is not None:
        raise FiberSupportStructureError("point asset mounts cannot define sequence")
    return normalized_type, normalized_role, None


def _assert_attach_available(
    db: Session,
    *,
    support_id: uuid.UUID,
    asset_type: str,
    asset_id: uuid.UUID,
    sequence: int | None,
) -> None:
    exact_edge = db.scalar(
        select(FiberSupportMount.id).where(
            FiberSupportMount.support_structure_id == support_id,
            FiberSupportMount.mounted_asset_type == asset_type,
            FiberSupportMount.mounted_asset_id == asset_id,
            FiberSupportMount.is_active.is_(True),
        )
    )
    if exact_edge is not None:
        raise FiberSupportStructureError("the exact support mount already exists")
    point_edge = (
        db.scalar(
            select(FiberSupportMount.id).where(
                FiberSupportMount.mounted_asset_type == asset_type,
                FiberSupportMount.mounted_asset_id == asset_id,
                FiberSupportMount.is_active.is_(True),
            )
        )
        if asset_type != "fiber_segment"
        else None
    )
    if point_edge is not None:
        raise FiberSupportStructureError(
            "the point asset already has an active support mount"
        )
    sequence_edge = (
        db.scalar(
            select(FiberSupportMount.id).where(
                FiberSupportMount.mounted_asset_type == asset_type,
                FiberSupportMount.mounted_asset_id == asset_id,
                FiberSupportMount.sequence == sequence,
                FiberSupportMount.is_active.is_(True),
            )
        )
        if asset_type == "fiber_segment"
        else None
    )
    if sequence_edge is not None:
        raise FiberSupportStructureError(
            "the fiber segment already has an active mount at this sequence"
        )


def preview_mount_decision(
    db: Session,
    *,
    action: str,
    support_structure_id: object,
    mounted_asset_type: str,
    mounted_asset_id: object,
    mount_role: str,
    sequence: int | None,
    existing_mount_id: object | None,
    reason: str,
    proposed_by: str,
    require_new: bool = False,
) -> FiberSupportMountPreview:
    """Return a write-free exact preview for one canonical mount transition."""

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"attach", "detach"}:
        raise FiberSupportStructureError("mount action is unsupported")
    actor = _text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _text(reason, "reason", limit=4000)
    support_id = _uuid(support_structure_id, "support_structure_id")
    asset_id = _uuid(mounted_asset_id, "mounted_asset_id")
    asset_type, role, normalized_sequence = _normalize_mount_shape(
        asset_type=mounted_asset_type,
        mount_role=mount_role,
        sequence=sequence,
    )
    support = _load_support(db, support_id)
    asset = _load_asset(db, asset_type, asset_id)
    mount: FiberSupportMount | None = None
    if normalized_action == "attach":
        if existing_mount_id is not None:
            raise FiberSupportStructureError(
                "existing_mount_id is only valid for detach"
            )
        _assert_attach_available(
            db,
            support_id=support_id,
            asset_type=asset_type,
            asset_id=asset_id,
            sequence=normalized_sequence,
        )
    else:
        if existing_mount_id is None:
            raise FiberSupportStructureError("detach requires existing_mount_id")
        mount = _load_mount(db, _uuid(existing_mount_id, "existing_mount_id"))
        if not mount.is_active:
            raise FiberSupportStructureError("support mount is already inactive")
        if (
            mount.support_structure_id != support_id
            or mount.mounted_asset_type != asset_type
            or mount.mounted_asset_id != asset_id
            or mount.mount_role != role
            or mount.sequence != normalized_sequence
        ):
            raise FiberSupportStructureError(
                "detach inputs do not match the exact active mount"
            )
    support_sha = _digest(_support_state(support))
    asset_sha = _digest(_asset_state(asset_type, asset))
    mount_sha = _digest(_mount_state(mount)) if mount is not None else None
    payload = {
        "action": normalized_action,
        "existing_mount_id": str(mount.id) if mount else None,
        "expected_asset_state_sha256": asset_sha,
        "expected_mount_state_sha256": mount_sha,
        "expected_support_state_sha256": support_sha,
        "mount_role": role,
        "mounted_asset_id": str(asset_id),
        "mounted_asset_type": asset_type,
        "proposed_by": actor,
        "reason": normalized_reason,
        "sequence": normalized_sequence,
        "support_structure_id": str(support_id),
    }
    decision_sha = _digest(payload)
    existing = db.scalar(
        select(FiberSupportMountDecision).where(
            FiberSupportMountDecision.decision_sha256 == decision_sha
        )
    )
    if require_new and existing is not None:
        raise FiberSupportStructureError("the exact mount decision already exists")
    overlap_conditions = [
        FiberSupportMountDecision.status.in_(ACTIVE_DECISION_STATUSES),
        FiberSupportMountDecision.mounted_asset_type == asset_type,
        FiberSupportMountDecision.mounted_asset_id == asset_id,
    ]
    if existing is not None:
        overlap_conditions.append(FiberSupportMountDecision.id != existing.id)
    overlapping = db.scalar(
        select(FiberSupportMountDecision).where(*overlap_conditions)
    )
    if overlapping is not None:
        raise FiberSupportStructureError(
            "an active mount decision already covers this canonical asset"
        )
    return FiberSupportMountPreview(
        action=normalized_action,
        support_structure_id=support_id,
        mounted_asset_type=asset_type,
        mounted_asset_id=asset_id,
        mount_role=role,
        sequence=normalized_sequence,
        existing_mount_id=mount.id if mount else None,
        expected_support_state_sha256=support_sha,
        expected_asset_state_sha256=asset_sha,
        expected_mount_state_sha256=mount_sha,
        reason=normalized_reason,
        proposed_by=actor,
        decision_sha256=decision_sha,
        existing_decision_id=existing.id if existing else None,
    )


def propose_mount_decision(
    db: Session,
    *,
    expected_decision_sha256: object,
    commit: bool = True,
    **preview_args: Any,
) -> FiberSupportMountDecision:
    """Confirm an exact preview and persist immutable proposal evidence."""

    expected = _sha256(expected_decision_sha256, "expected_decision_sha256")
    preview = preview_mount_decision(db, **preview_args)
    if preview.decision_sha256 != expected:
        raise FiberSupportStructureError("mount decision preview changed")
    if preview.existing_decision_id is not None:
        existing = db.get(FiberSupportMountDecision, preview.existing_decision_id)
        if existing is not None:
            return existing
    row = FiberSupportMountDecision(
        action=preview.action,
        support_structure_id=preview.support_structure_id,
        mounted_asset_type=preview.mounted_asset_type,
        mounted_asset_id=preview.mounted_asset_id,
        mount_role=preview.mount_role,
        sequence=preview.sequence,
        existing_mount_id=preview.existing_mount_id,
        expected_support_state_sha256=preview.expected_support_state_sha256,
        expected_asset_state_sha256=preview.expected_asset_state_sha256,
        expected_mount_state_sha256=preview.expected_mount_state_sha256,
        reason=preview.reason,
        proposed_by=preview.proposed_by,
        status="proposed",
        decision_sha256=preview.decision_sha256,
    )
    db.add(row)
    db.flush()
    _audit(db, row, "fiber_support_mount.proposed", preview.proposed_by)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def _load_decision(
    db: Session, decision_id: object, *, lock: bool = False
) -> FiberSupportMountDecision:
    normalized = _uuid(decision_id, "decision_id")
    stmt = select(FiberSupportMountDecision).where(
        FiberSupportMountDecision.id == normalized
    )
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None:
        raise FiberSupportStructureError("support mount decision not found")
    return row


def _decision_preview(
    db: Session,
    row: FiberSupportMountDecision,
    *,
    lock: bool = False,
) -> FiberSupportMountPreview:
    support = _load_support(db, row.support_structure_id, lock=lock)
    asset = _load_asset(db, row.mounted_asset_type, row.mounted_asset_id, lock=lock)
    mount: FiberSupportMount | None = None
    if row.action == "attach":
        _assert_attach_available(
            db,
            support_id=row.support_structure_id,
            asset_type=row.mounted_asset_type,
            asset_id=row.mounted_asset_id,
            sequence=row.sequence,
        )
    else:
        if row.existing_mount_id is None:
            raise FiberSupportStructureError("detach decision has no exact mount")
        mount = _load_mount(db, row.existing_mount_id, lock=lock)
        if (
            not mount.is_active
            or mount.support_structure_id != row.support_structure_id
            or mount.mounted_asset_type != row.mounted_asset_type
            or mount.mounted_asset_id != row.mounted_asset_id
            or mount.mount_role != row.mount_role
            or mount.sequence != row.sequence
        ):
            raise FiberSupportStructureError(
                "support or mounted-asset evidence changed"
            )
    support_sha = _digest(_support_state(support))
    asset_sha = _digest(_asset_state(row.mounted_asset_type, asset))
    mount_sha = _digest(_mount_state(mount)) if mount is not None else None
    if (
        support_sha != row.expected_support_state_sha256
        or asset_sha != row.expected_asset_state_sha256
        or mount_sha != row.expected_mount_state_sha256
    ):
        raise FiberSupportStructureError("support or mounted-asset evidence changed")
    payload = {
        "action": row.action,
        "existing_mount_id": str(mount.id) if mount else None,
        "expected_asset_state_sha256": asset_sha,
        "expected_mount_state_sha256": mount_sha,
        "expected_support_state_sha256": support_sha,
        "mount_role": row.mount_role,
        "mounted_asset_id": str(row.mounted_asset_id),
        "mounted_asset_type": row.mounted_asset_type,
        "proposed_by": row.proposed_by,
        "reason": row.reason,
        "sequence": row.sequence,
        "support_structure_id": str(row.support_structure_id),
    }
    if _digest(payload) != row.decision_sha256:
        raise FiberSupportStructureError("mount decision evidence is invalid")
    return FiberSupportMountPreview(
        action=row.action,
        support_structure_id=row.support_structure_id,
        mounted_asset_type=row.mounted_asset_type,
        mounted_asset_id=row.mounted_asset_id,
        mount_role=row.mount_role,
        sequence=row.sequence,
        existing_mount_id=row.existing_mount_id,
        expected_support_state_sha256=support_sha,
        expected_asset_state_sha256=asset_sha,
        expected_mount_state_sha256=mount_sha,
        reason=row.reason,
        proposed_by=row.proposed_by,
        decision_sha256=row.decision_sha256,
        existing_decision_id=row.id,
    )


def review_mount_decision(
    db: Session,
    decision_id: object,
    *,
    action: str,
    reviewed_by: str,
    review_notes: str,
    expected_decision_sha256: object,
    commit: bool = True,
) -> FiberSupportMountDecision:
    """Independently approve or decline the unchanged exact proposal."""

    expected = _sha256(expected_decision_sha256, "expected_decision_sha256")
    actor = _text(reviewed_by, "reviewed_by", limit=160)
    notes = _text(review_notes, "review_notes", limit=4000)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"approve", "decline"}:
        raise FiberSupportStructureError("review action is unsupported")
    row = _load_decision(db, decision_id, lock=True)
    if row.decision_sha256 != expected:
        raise FiberSupportStructureError("mount decision confirmation is stale")
    target_status = "approved" if normalized_action == "approve" else "declined"
    if (
        row.status == target_status
        and row.reviewed_by == actor
        and row.review_notes == notes
    ):
        return row
    if row.status != "proposed":
        raise FiberSupportStructureError("mount decision is not awaiting review")
    if row.proposed_by == actor:
        raise FiberSupportStructureError(
            "the proposer cannot review this mount decision"
        )
    if normalized_action == "approve":
        _decision_preview(db, row, lock=True)
    row.status = target_status
    row.reviewed_by = actor
    row.review_notes = notes
    row.reviewed_at = datetime.now(UTC)
    if target_status == "declined":
        row.closed_reason = "mount_decision_declined"
    _audit(db, row, f"fiber_support_mount.{target_status}", actor)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def _base_result(
    row: FiberSupportMountDecision, *, actor: str, outcome: str
) -> dict[str, object]:
    return {
        "action": row.action,
        "decision_id": str(row.id),
        "executed_by": actor,
        "mount_role": row.mount_role,
        "mounted_asset_id": str(row.mounted_asset_id),
        "mounted_asset_type": row.mounted_asset_type,
        "outcome": outcome,
        "schema_version": 1,
        "sequence": row.sequence,
        "support_structure_id": str(row.support_structure_id),
    }


def _set_result(
    row: FiberSupportMountDecision,
    *,
    actor: str,
    status: str,
    payload: dict[str, object],
    result_mount_id: uuid.UUID | None = None,
    closed_reason: str | None = None,
) -> None:
    row.status = status
    row.executed_by = actor
    row.executed_at = datetime.now(UTC)
    row.closed_reason = closed_reason
    row.result_mount_id = result_mount_id
    row.result_payload = payload
    row.result_sha256 = _digest(payload)


def _finish_execution(
    db: Session,
    row: FiberSupportMountDecision,
    *,
    actor: str,
    commit: bool,
) -> FiberSupportMountDecision:
    _audit(
        db,
        row,
        f"fiber_support_mount.{row.status}",
        actor,
        metadata={
            "result": row.result_payload,
            "result_sha256": row.result_sha256,
        },
    )
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def execute_mount_decision(
    db: Session,
    decision_id: object,
    *,
    executed_by: str,
    expected_decision_sha256: object,
    commit: bool = True,
) -> FiberSupportMountDecision:
    """Lock, revalidate, and apply the exact independently reviewed edge."""

    expected = _sha256(expected_decision_sha256, "expected_decision_sha256")
    actor = _text(executed_by, "executed_by", limit=160)
    row = _load_decision(db, decision_id, lock=True)
    if row.decision_sha256 != expected:
        raise FiberSupportStructureError("mount decision confirmation is stale")
    if row.status in {"applied", "closed"}:
        return row
    if row.status != "approved":
        raise FiberSupportStructureError("mount decision is not approved")
    try:
        _decision_preview(db, row, lock=True)
    except FiberSupportStructureError as exc:
        result = _base_result(row, actor=actor, outcome="closed_stale")
        result["error"] = str(exc)
        _set_result(
            row,
            actor=actor,
            status="closed",
            payload=result,
            closed_reason="authoritative_support_or_asset_inputs_changed",
        )
        return _finish_execution(db, row, actor=actor, commit=commit)
    now = datetime.now(UTC)
    if row.action == "attach":
        mount = FiberSupportMount(
            decision_id=row.id,
            support_structure_id=row.support_structure_id,
            mounted_asset_type=row.mounted_asset_type,
            mounted_asset_id=row.mounted_asset_id,
            mount_role=row.mount_role,
            sequence=row.sequence,
            is_active=True,
            installed_by=actor,
            installed_at=now,
            notes=row.reason,
        )
        try:
            with db.begin_nested():
                db.add(mount)
                db.flush()
        except IntegrityError:
            result = _base_result(row, actor=actor, outcome="closed_conflict")
            result["error"] = "canonical support mount uniqueness conflict"
            _set_result(
                row,
                actor=actor,
                status="closed",
                payload=result,
                closed_reason="canonical_support_mount_conflict",
            )
            return _finish_execution(db, row, actor=actor, commit=commit)
    else:
        assert row.existing_mount_id is not None
        mount = _load_mount(db, row.existing_mount_id, lock=True)
        mount.is_active = False
        mount.removed_by = actor
        mount.removed_at = now
        db.flush()
    result_payload = _base_result(row, actor=actor, outcome="applied")
    result_payload["mount"] = _mount_state(mount)
    _set_result(
        row,
        actor=actor,
        status="applied",
        payload=result_payload,
        result_mount_id=mount.id,
    )
    return _finish_execution(db, row, actor=actor, commit=commit)


def inspect_mount_decision(db: Session, decision_id: object) -> dict[str, object]:
    row = _load_decision(db, decision_id)
    result_valid = (
        None
        if row.result_payload is None and row.result_sha256 is None
        else bool(
            row.result_payload is not None
            and row.result_sha256 is not None
            and _digest(row.result_payload) == row.result_sha256
        )
    )
    result_current = None
    if row.status == "applied" and row.result_mount_id is not None:
        mount = db.get(FiberSupportMount, row.result_mount_id)
        expected_mount = (
            row.result_payload.get("mount")
            if isinstance(row.result_payload, dict)
            else None
        )
        result_current = bool(
            mount is not None
            and isinstance(expected_mount, dict)
            and _mount_state(mount) == expected_mount
        )
    return {
        "action": row.action,
        "decision_id": str(row.id),
        "decision_sha256": row.decision_sha256,
        "existing_mount_id": str(row.existing_mount_id)
        if row.existing_mount_id
        else None,
        "mount_role": row.mount_role,
        "mounted_asset_id": str(row.mounted_asset_id),
        "mounted_asset_type": row.mounted_asset_type,
        "proposed_by": row.proposed_by,
        "reviewed_by": row.reviewed_by,
        "executed_by": row.executed_by,
        "result_mount_id": str(row.result_mount_id) if row.result_mount_id else None,
        "result_payload": row.result_payload,
        "result_sha256": row.result_sha256,
        "result_current": result_current,
        "result_valid": result_valid,
        "sequence": row.sequence,
        "status": row.status,
        "support_structure_id": str(row.support_structure_id),
    }


def _audit(
    db: Session,
    row: FiberSupportMountDecision,
    action: str,
    actor: str,
    *,
    metadata: dict[str, object] | None = None,
) -> None:
    stage_audit_event(
        db,
        action=action,
        entity_type="fiber_support_mount_decision",
        entity_id=str(row.id),
        actor_type=AuditActorType.system,
        metadata={
            "actor": actor,
            "decision_sha256": row.decision_sha256,
            "owner": "network.fiber_support_structures",
            "status": row.status,
            **(metadata or {}),
        },
    )


__all__ = [
    "MOUNTED_ASSET_MODELS",
    "FiberSupportMountPreview",
    "FiberSupportStructureError",
    "apply_reviewed_support_change",
    "execute_mount_decision",
    "inspect_mount_decision",
    "preview_mount_decision",
    "propose_mount_decision",
    "review_mount_decision",
]
