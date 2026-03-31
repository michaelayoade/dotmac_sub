"""Shared helpers for ONT and CPE actions executed through GenieACS."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import CPEDevice, OntUnit
from app.services.network._resolve import (
    resolve_genieacs_for_cpe_with_reason,
    resolve_genieacs_with_reason,
)

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Result of a remote ONT action."""

    success: bool
    message: str
    data: dict[str, Any] | None = None
    waiting: bool = False


@dataclass
class DeviceConfig:
    """Structured running config from an ONT."""

    device_info: dict[str, Any]
    wan: dict[str, Any]
    optical: dict[str, Any]
    wifi: dict[str, Any]
    raw: dict[str, Any]


TR069_ROOT_DEVICE = "Device"
TR069_ROOT_IGD = "InternetGatewayDevice"


def detect_data_model_root(
    db: Session,
    ont: OntUnit | CPEDevice,
    client: Any,
    device_id: str,
) -> str:
    """Detect whether device uses Device (TR-181) or InternetGatewayDevice (TR-098).

    Checks the cached value on the model first, then queries GenieACS.
    Caches the result on the loaded model instance for reuse in the current
    unit of work. Persistence is left to explicit write paths.
    """
    if ont.tr069_data_model in (TR069_ROOT_DEVICE, TR069_ROOT_IGD):
        return ont.tr069_data_model

    try:
        from app.services.genieacs import GenieACSError

        device = client.get_device(device_id)
        if isinstance(device.get("Device"), dict):
            root = TR069_ROOT_DEVICE
        else:
            root = TR069_ROOT_IGD
        ont.tr069_data_model = root
        return root
    except GenieACSError as exc:
        logger.warning(
            "Could not detect data model for ONT %s, defaulting to IGD: %s",
            ont.serial_number,
            exc,
        )
        return TR069_ROOT_IGD


def persist_data_model_root(
    device: OntUnit | CPEDevice,
    root: str,
) -> None:
    """Persist a detected data-model root in an isolated transaction."""
    if root not in (TR069_ROOT_DEVICE, TR069_ROOT_IGD):
        return
    device_id = getattr(device, "id", None)
    if not device_id:
        return

    model_cls = type(device)

    try:
        from app.db import SessionLocal

        db = SessionLocal()
        try:
            record = db.get(model_cls, str(device_id))
            if record is None:
                return
            current = getattr(record, "tr069_data_model", None)
            if current == root:
                return
            record.tr069_data_model = root  # type: ignore[attr-defined]
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning(
            "Failed to persist TR-069 data model root %s for %s:%s",
            root,
            model_cls.__name__,
            device_id,
            exc_info=True,
        )


def build_tr069_params(
    root: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Build parameter dict using only the detected data model root.

    Args:
        root: "Device" or "InternetGatewayDevice"
        params: Dict of {suffix_path: value} where suffix_path omits the root.
                Example: {"ManagementServer.ConnectionRequestUsername": "acs"}

    Returns:
        Dict of {full_path: value} with the correct root prefix.
    """
    return {f"{root}.{path}": value for path, value in params.items()}


def get_ont_or_error(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, ActionResult | None]:
    """Load an ONT record or return a standard not-found result."""
    ont = db.get(OntUnit, ont_id)
    if not ont:
        return None, ActionResult(success=False, message="ONT not found.")
    return ont, None


def resolve_client_or_error(
    db: Session,
    ont: OntUnit,
) -> tuple[tuple[Any, str] | None, ActionResult | None]:
    """Resolve the GenieACS client/device pair for an ONT."""
    resolved, reason = resolve_genieacs_with_reason(db, ont)
    if not resolved:
        return None, ActionResult(
            success=False,
            message=reason or "No GenieACS server configured for this ONT.",
        )
    return resolved, None


def get_cpe_or_error(
    db: Session, cpe_id: str
) -> tuple[CPEDevice | None, ActionResult | None]:
    """Load a CPE device record or return a standard not-found result."""
    cpe = db.get(CPEDevice, cpe_id)
    if not cpe:
        return None, ActionResult(success=False, message="CPE device not found.")
    return cpe, None


def resolve_cpe_client_or_error(
    db: Session,
    cpe: CPEDevice,
) -> tuple[tuple[Any, str] | None, ActionResult | None]:
    """Resolve the GenieACS client/device pair for a CPE device."""
    resolved, reason = resolve_genieacs_for_cpe_with_reason(db, cpe)
    if not resolved:
        return None, ActionResult(
            success=False,
            message=reason or "No GenieACS server configured for this CPE device.",
        )
    return resolved, None


def get_ont_strict_or_error(
    db: Session, ont_id: str
) -> tuple[OntUnit | None, ActionResult | None]:
    """Load an ONT and narrow away the optional type for callers."""
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return None, error
    if ont is None:
        return None, ActionResult(success=False, message="ONT not found.")
    return ont, None


def get_ont_client_or_error(
    db: Session, ont_id: str
) -> tuple[tuple[OntUnit, Any, str] | None, ActionResult | None]:
    """Load an ONT and resolve its GenieACS client/device id."""
    ont, error = get_ont_strict_or_error(db, ont_id)
    if error:
        return None, error
    if ont is None:
        return None, ActionResult(success=False, message="ONT not found.")
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return None, error
    if resolved is None:
        return None, ActionResult(
            success=False,
            message="No GenieACS server configured for this ONT.",
        )
    client, device_id = resolved
    return (ont, client, device_id), None


def get_cpe_strict_or_error(
    db: Session, cpe_id: str
) -> tuple[CPEDevice | None, ActionResult | None]:
    """Load a CPE and narrow away the optional type for callers."""
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return None, error
    if cpe is None:
        return None, ActionResult(success=False, message="CPE device not found.")
    return cpe, None


def get_cpe_client_or_error(
    db: Session, cpe_id: str
) -> tuple[tuple[CPEDevice, Any, str] | None, ActionResult | None]:
    """Load a CPE and resolve its GenieACS client/device id."""
    cpe, error = get_cpe_strict_or_error(db, cpe_id)
    if error:
        return None, error
    if cpe is None:
        return None, ActionResult(success=False, message="CPE device not found.")
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return None, error
    if resolved is None:
        return None, ActionResult(
            success=False,
            message="No GenieACS server configured for this CPE device.",
        )
    client, device_id = resolved
    return (cpe, client, device_id), None
