"""Shared helpers for ONT actions executed through GenieACS."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.network._resolve import resolve_genieacs_with_reason

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Result of a remote ONT action."""

    success: bool
    message: str
    data: dict[str, Any] | None = None


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
    ont: OntUnit,
    client: Any,
    device_id: str,
) -> str:
    """Detect whether ONT uses Device (TR-181) or InternetGatewayDevice (TR-098).

    Checks the cached value on OntUnit first, then queries GenieACS.
    Caches the result on the OntUnit model for future use.
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
        db.flush()
        return root
    except GenieACSError as exc:
        logger.warning(
            "Could not detect data model for ONT %s, defaulting to IGD: %s",
            ont.serial_number,
            exc,
        )
        return TR069_ROOT_IGD


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


def get_ont_or_error(db: Session, ont_id: str) -> tuple[OntUnit | None, ActionResult | None]:
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
