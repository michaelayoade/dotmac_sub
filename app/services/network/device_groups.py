"""Device group services for batch device operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.network import CPEDevice, DeviceGroup, DeviceGroupMember, OntUnit

DEVICE_GROUP_MEMBER_TYPES = {"ont", "cpe"}
ONT_GROUP_ACTIONS = {
    "reboot",
    "factory_reset",
    "speed_update",
    "catv_toggle",
    "wifi_update",
    "voip_toggle",
    "provision",
}


class DeviceGroupError(ValueError):
    """Raised when a device group request is invalid."""


def create_device_group(
    db: Session,
    *,
    name: str,
    kind: str = "manual",
    description: str | None = None,
    criteria: dict[str, Any] | None = None,
    created_by: str | None = None,
) -> DeviceGroup:
    """Create a device group."""
    name_value = str(name or "").strip()
    if not name_value:
        raise DeviceGroupError("Device group name is required")
    group = DeviceGroup(
        name=name_value[:120],
        kind=str(kind or "manual").strip()[:40] or "manual",
        description=str(description).strip() if description else None,
        criteria=criteria,
        created_by=str(created_by).strip()[:120] if created_by else None,
        is_active=True,
    )
    db.add(group)
    db.flush()
    return group


def list_device_groups(db: Session, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Return device groups with member counts for the admin UI."""
    query = select(DeviceGroup).order_by(DeviceGroup.name.asc())
    if not include_inactive:
        query = query.where(DeviceGroup.is_active.is_(True))
    groups = list(db.scalars(query))
    if not groups:
        return []
    counts = {
        (row.group_id, row.device_type): int(row.count)
        for row in db.execute(
            select(
                DeviceGroupMember.group_id,
                DeviceGroupMember.device_type,
                func.count(DeviceGroupMember.id).label("count"),
            )
            .where(DeviceGroupMember.group_id.in_([group.id for group in groups]))
            .group_by(DeviceGroupMember.group_id, DeviceGroupMember.device_type)
        ).all()
    }
    return [
        {
            "group": group,
            "ont_count": counts.get((group.id, "ont"), 0),
            "cpe_count": counts.get((group.id, "cpe"), 0),
            "member_count": sum(
                counts.get((group.id, device_type), 0)
                for device_type in DEVICE_GROUP_MEMBER_TYPES
            ),
        }
        for group in groups
    ]


def device_group_detail_context(db: Session, group_id: str | UUID) -> dict[str, Any]:
    """Return a device group with display-ready members."""
    group = _get_active_group(db, group_id)
    members = list(
        db.scalars(
            select(DeviceGroupMember)
            .where(DeviceGroupMember.group_id == group.id)
            .order_by(DeviceGroupMember.added_at.desc())
        )
    )
    ont_ids = [member.device_id for member in members if member.device_type == "ont"]
    cpe_ids = [member.device_id for member in members if member.device_type == "cpe"]
    onts = (
        {
            ont.id: ont
            for ont in db.scalars(select(OntUnit).where(OntUnit.id.in_(ont_ids))).all()
        }
        if ont_ids
        else {}
    )
    cpes = (
        {
            cpe.id: cpe
            for cpe in db.scalars(
                select(CPEDevice).where(CPEDevice.id.in_(cpe_ids))
            ).all()
        }
        if cpe_ids
        else {}
    )
    rows = []
    for member in members:
        device = (
            onts.get(member.device_id)
            if member.device_type == "ont"
            else cpes.get(member.device_id)
        )
        rows.append(
            {
                "member": member,
                "device": device,
                "label": _device_label(member.device_type, device, member.device_id),
            }
        )
    return {
        "group": group,
        "member_rows": rows,
        "ont_count": len(ont_ids),
        "cpe_count": len(cpe_ids),
    }


def add_device_group_member(
    db: Session,
    *,
    group_id: str | UUID,
    device_type: str,
    device_id: str | UUID,
    added_by: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DeviceGroupMember:
    """Add one ONT or CPE to a group, returning an existing row if present."""
    group = _get_active_group(db, group_id)
    normalized_type = _normalize_member_type(device_type)
    normalized_id = _validate_device_exists(db, normalized_type, device_id)

    existing = db.scalars(
        select(DeviceGroupMember)
        .where(DeviceGroupMember.group_id == group.id)
        .where(DeviceGroupMember.device_type == normalized_type)
        .where(DeviceGroupMember.device_id == normalized_id)
        .limit(1)
    ).first()
    if existing is not None:
        return existing

    member = DeviceGroupMember(
        group_id=group.id,
        device_type=normalized_type,
        device_id=normalized_id,
        added_by=str(added_by).strip()[:120] if added_by else None,
        metadata_json=metadata,
    )
    db.add(member)
    db.flush()
    return member


def remove_device_group_member(
    db: Session,
    *,
    group_id: str | UUID,
    member_id: str | UUID,
) -> None:
    """Remove one member from a group."""
    group = _get_active_group(db, group_id)
    try:
        member_uuid = UUID(str(member_id))
    except (TypeError, ValueError) as exc:
        raise DeviceGroupError("Invalid member id") from exc
    member = db.get(DeviceGroupMember, member_uuid)
    if member is None or member.group_id != group.id:
        raise DeviceGroupError("Device group member not found")
    db.delete(member)
    db.flush()


def list_device_group_ont_ids(db: Session, group_id: str | UUID) -> list[str]:
    """Return active ONT IDs in a group."""
    group = _get_active_group(db, group_id)
    rows = db.scalars(
        select(DeviceGroupMember.device_id)
        .join(OntUnit, OntUnit.id == DeviceGroupMember.device_id)
        .where(DeviceGroupMember.group_id == group.id)
        .where(DeviceGroupMember.device_type == "ont")
        .where(OntUnit.is_active.is_(True))
        .order_by(DeviceGroupMember.added_at.asc())
    ).all()
    return [str(row) for row in rows]


def enqueue_ont_group_action(
    db: Session,
    *,
    group_id: str | UUID,
    action: str,
    params: dict[str, Any] | None = None,
    initiated_by: str | None = None,
) -> dict[str, Any]:
    """Queue a bulk action for all ONTs in a group."""
    action_value = str(action or "").strip()
    if action_value not in ONT_GROUP_ACTIONS:
        raise DeviceGroupError(f"Unsupported ONT group action: {action_value}")
    ont_ids = list_device_group_ont_ids(db, group_id)
    if not ont_ids:
        raise DeviceGroupError("Device group has no active ONT members")

    from app.tasks.ont_bulk import execute_bulk_action

    payload = dict(params or {})
    if initiated_by:
        payload["initiated_by"] = initiated_by
    task = execute_bulk_action.delay(ont_ids, action_value, payload)
    return {
        "group_id": str(group_id),
        "action": action_value,
        "ont_count": len(ont_ids),
        "task_id": getattr(task, "id", None),
    }


def _get_active_group(db: Session, group_id: str | UUID) -> DeviceGroup:
    try:
        group_uuid = UUID(str(group_id))
    except (TypeError, ValueError) as exc:
        raise DeviceGroupError("Invalid device group id") from exc
    group = db.get(DeviceGroup, group_uuid)
    if group is None or not group.is_active:
        raise DeviceGroupError("Device group not found")
    return group


def _normalize_member_type(device_type: str) -> str:
    value = str(device_type or "").strip().lower()
    if value not in DEVICE_GROUP_MEMBER_TYPES:
        raise DeviceGroupError(f"Unsupported device group member type: {value}")
    return value


def _validate_device_exists(
    db: Session,
    device_type: str,
    device_id: str | UUID,
) -> UUID:
    try:
        device_uuid = UUID(str(device_id))
    except (TypeError, ValueError) as exc:
        raise DeviceGroupError("Invalid device id") from exc
    model = OntUnit if device_type == "ont" else CPEDevice
    if db.get(model, device_uuid) is None:
        raise DeviceGroupError(f"{device_type.upper()} device not found")
    return device_uuid


def _device_label(device_type: str, device: Any, fallback_id: UUID) -> str:
    if device_type == "ont":
        return str(getattr(device, "serial_number", None) or fallback_id)
    return str(
        getattr(device, "serial_number", None)
        or getattr(device, "mac_address", None)
        or fallback_id
    )
