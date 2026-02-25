"""Service helpers for admin TR-069 web routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.network import CPEDevice
from app.models.tr069 import Tr069CpeDevice, Tr069JobStatus
from app.schemas.tr069 import (
    Tr069AcsServerCreate,
    Tr069AcsServerUpdate,
    Tr069CpeDeviceUpdate,
    Tr069JobCreate,
)
from app.services import tr069 as tr069_service
from app.services.common import coerce_uuid


@dataclass
class JobAction:
    label: str
    command: str
    payload: dict | None = None


_JOB_ACTIONS: dict[str, JobAction] = {
    "refresh": JobAction(
        label="Refresh Parameters",
        command="refreshObject",
        payload={"objectName": "Device."},
    ),
    "reboot": JobAction(label="Reboot Device", command="reboot"),
    "factory_reset": JobAction(label="Factory Reset", command="factoryReset"),
}


def parse_acs_form(form) -> dict[str, object]:
    return {
        "name": str(form.get("name") or "").strip(),
        "base_url": str(form.get("base_url") or "").strip(),
        "is_active": str(form.get("is_active") or "true").strip().lower() in ("1", "true", "on", "yes"),
        "notes": str(form.get("notes") or "").strip() or None,
    }


def validate_acs_values(values: dict[str, object]) -> str | None:
    if not values.get("name"):
        return "ACS server name is required."
    if not values.get("base_url"):
        return "ACS base URL is required."
    return None


def acs_form_snapshot(values: dict[str, object], *, acs_id: str | None = None) -> dict[str, object]:
    data = dict(values)
    if acs_id:
        data["id"] = acs_id
    return data


def acs_form_snapshot_from_model(server) -> dict[str, object]:
    return {
        "id": str(server.id),
        "name": server.name,
        "base_url": server.base_url,
        "is_active": bool(server.is_active),
        "notes": server.notes or "",
    }


def create_acs_server(db: Session, values: dict[str, object]):
    payload = Tr069AcsServerCreate.model_validate(values)
    return tr069_service.acs_servers.create(db=db, payload=payload)


def update_acs_server(db: Session, *, acs_id: str, values: dict[str, object]):
    payload = Tr069AcsServerUpdate.model_validate(values)
    return tr069_service.acs_servers.update(db=db, server_id=acs_id, payload=payload)


def get_acs_server(db: Session, *, acs_id: str):
    return tr069_service.acs_servers.get(db=db, server_id=acs_id)


def queue_device_job(db: Session, *, tr069_device_id: str, action: str):
    selected = _JOB_ACTIONS.get(action)
    if selected is None:
        raise ValueError("Unsupported TR-069 action.")

    payload = Tr069JobCreate(
        device_id=coerce_uuid(tr069_device_id),
        name=selected.label,
        command=selected.command,
        payload=selected.payload,
    )
    job = tr069_service.jobs.create(db=db, payload=payload)
    return tr069_service.jobs.execute(db=db, job_id=str(job.id))


def link_tr069_device_to_cpe(
    db: Session,
    *,
    tr069_device_id: str,
    cpe_device_id: str | None,
):
    payload = Tr069CpeDeviceUpdate(
        cpe_device_id=coerce_uuid(cpe_device_id) if cpe_device_id else None,
    )
    return tr069_service.cpe_devices.update(
        db=db,
        device_id=tr069_device_id,
        payload=payload,
    )


def tr069_dashboard_data(
    db: Session,
    *,
    acs_server_id: str | None = None,
    search: str | None = None,
    only_unlinked: bool = False,
) -> dict[str, object]:
    servers = tr069_service.acs_servers.list(
        db=db,
        is_active=None,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    selected_server_id = str(acs_server_id or "").strip() or None
    if not selected_server_id and servers:
        selected_server_id = str(servers[0].id)

    devices = tr069_service.cpe_devices.list(
        db=db,
        acs_server_id=selected_server_id,
        is_active=None,
        order_by="serial_number",
        order_dir="asc",
        limit=5000,
        offset=0,
    ) if selected_server_id else []

    search_q = str(search or "").strip().lower()
    if search_q:
        devices = [
            item for item in devices
            if search_q in " ".join(
                [
                    str(item.serial_number or ""),
                    str(item.oui or ""),
                    str(item.product_class or ""),
                    str(item.connection_request_url or ""),
                ]
            ).lower()
        ]

    if only_unlinked:
        devices = [item for item in devices if not item.cpe_device_id]

    linked_cpe_ids = [item.cpe_device_id for item in devices if item.cpe_device_id]
    linked_cpes = (
        db.query(CPEDevice)
        .filter(CPEDevice.id.in_(linked_cpe_ids))
        .all()
        if linked_cpe_ids
        else []
    )
    cpe_by_id = {str(cpe.id): cpe for cpe in linked_cpes}

    for device in devices:
        setattr(device, "linked_cpe", cpe_by_id.get(str(device.cpe_device_id)) if device.cpe_device_id else None)

    jobs = tr069_service.jobs.list(
        db=db,
        device_id=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=25,
        offset=0,
    )

    managed_cpes = (
        db.query(CPEDevice)
        .order_by(CPEDevice.created_at.desc())
        .limit(1000)
        .all()
    )

    now = datetime.now(UTC)
    seen_window = now - timedelta(hours=24)

    def _seen_recently(item: Tr069CpeDevice) -> bool:
        informed_at = item.last_inform_at
        if informed_at is None:
            return False
        if informed_at.tzinfo is None:
            informed_at = informed_at.replace(tzinfo=UTC)
        return informed_at >= seen_window

    return {
        "servers": servers,
        "selected_server_id": selected_server_id or "",
        "devices": devices,
        "recent_jobs": jobs,
        "managed_cpes": managed_cpes,
        "job_actions": _JOB_ACTIONS,
        "stats": {
            "servers": len(servers),
            "devices": len(devices),
            "unlinked": sum(1 for item in devices if not item.cpe_device_id),
            "seen_24h": sum(1 for item in devices if _seen_recently(item)),
            "jobs_failed": sum(1 for item in jobs if item.status == Tr069JobStatus.failed),
        },
        "filters": {
            "search": str(search or "").strip(),
            "only_unlinked": bool(only_unlinked),
        },
    }


def sync_server(db: Session, *, acs_server_id: str) -> dict[str, int]:
    return tr069_service.cpe_devices.sync_from_genieacs(db=db, acs_server_id=acs_server_id)
