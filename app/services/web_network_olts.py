"""Service helpers for admin OLT web routes."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.models.network import OLTDevice
from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services import network as network_service


def integrity_error_message(exc: Exception) -> str:
    """Map OLT integrity errors to user-facing strings."""
    message = str(exc)
    if "uq_olt_devices_hostname" in message:
        return "Hostname already exists"
    if "uq_olt_devices_mgmt_ip" in message:
        return "Management IP already exists"
    return "OLT device could not be saved due to a data conflict"


def parse_form_values(form) -> dict[str, object]:
    """Parse OLT form values."""
    return {
        "name": form.get("name", "").strip(),
        "hostname": form.get("hostname", "").strip() or None,
        "mgmt_ip": form.get("mgmt_ip", "").strip() or None,
        "vendor": form.get("vendor", "").strip() or None,
        "model": form.get("model", "").strip() or None,
        "serial_number": form.get("serial_number", "").strip() or None,
        "notes": form.get("notes", "").strip() or None,
        "is_active": form.get("is_active") == "true",
    }


def validate_values(db, values: dict[str, object], *, current_olt=None) -> str | None:
    """Validate required fields and uniqueness."""
    if not values.get("name"):
        return "Name is required"
    hostname = values.get("hostname")
    mgmt_ip = values.get("mgmt_ip")
    if hostname:
        query = db.query(OLTDevice).filter(OLTDevice.hostname == hostname)
        if current_olt:
            query = query.filter(OLTDevice.id != current_olt.id)
        if query.first():
            return "Hostname already exists"
    if mgmt_ip:
        query = db.query(OLTDevice).filter(OLTDevice.mgmt_ip == mgmt_ip)
        if current_olt:
            query = query.filter(OLTDevice.id != current_olt.id)
        if query.first():
            return "Management IP already exists"
    return None


def create_payload(values: dict[str, object]) -> OLTDeviceCreate:
    """Build create payload from parsed values."""
    return OLTDeviceCreate.model_validate(values)


def update_payload(values: dict[str, object]) -> OLTDeviceUpdate:
    """Build update payload from parsed values."""
    return OLTDeviceUpdate.model_validate(values)


def create_olt(db, values: dict[str, object]):
    """Create OLT and normalize integrity errors."""
    try:
        return network_service.olt_devices.create(db=db, payload=create_payload(values)), None
    except IntegrityError as exc:
        db.rollback()
        return None, integrity_error_message(exc)


def update_olt(db, olt_id: str, values: dict[str, object]):
    """Update OLT and normalize integrity errors."""
    try:
        return network_service.olt_devices.update(
            db=db,
            device_id=olt_id,
            payload=update_payload(values),
        ), None
    except IntegrityError as exc:
        db.rollback()
        return None, integrity_error_message(exc)


def snapshot(values: dict[str, object]):
    """Build simple object for form re-render on errors."""
    return SimpleNamespace(**values)
