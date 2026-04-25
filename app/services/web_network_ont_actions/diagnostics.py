"""Diagnostic operations for ONT web actions."""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.services.network.ont_actions import ActionResult, OntActions
from app.services.web_network_ont_actions._common import _log_action_audit


def run_ping_diagnostic(
    db: Session,
    ont_id: str,
    host: str,
    count: int = 4,
    *,
    request: Request | None = None,
) -> ActionResult:
    """Run ping diagnostic from ONT via TR-069."""
    result = OntActions.run_ping_diagnostic(db, ont_id, host, count)
    _log_action_audit(
        db,
        request=request,
        action="ping_diagnostic",
        ont_id=ont_id,
        metadata={
            "result": "success" if result.success else "error",
            "host": host,
            "count": count,
        },
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def run_traceroute_diagnostic(
    db: Session, ont_id: str, host: str, *, request: Request | None = None
) -> ActionResult:
    """Run traceroute diagnostic from ONT via TR-069."""
    result = OntActions.run_traceroute_diagnostic(db, ont_id, host)
    _log_action_audit(
        db,
        request=request,
        action="traceroute_diagnostic",
        ont_id=ont_id,
        metadata={"result": "success" if result.success else "error", "host": host},
        status_code=200 if result.success else 500,
        is_success=result.success,
    )
    return result


def fetch_running_config(db: Session, ont_id: str) -> ActionResult:
    """Fetch running config and return structured result."""
    return OntActions.get_running_config(db, ont_id)


def fetch_iphost_config(db: Session, ont_id: str) -> tuple[bool, str, dict[str, str]]:
    """Get management IP config from ONT assignment.

    Management IP configuration is now stored in OntAssignment rather than
    polled from the OLT. This returns the configured values from the active
    assignment.
    """
    from app.services import network as network_service

    ont = network_service.ont_units.get_including_inactive(db, ont_id)
    if not ont:
        return False, "ONT not found", {}

    # Find active assignment
    active_assignment = None
    for assignment in getattr(ont, "assignments", []):
        if getattr(assignment, "active", False):
            active_assignment = assignment
            break

    if not active_assignment:
        return True, "No active assignment - management IP not configured", {}

    mgmt_ip_mode = getattr(active_assignment, "mgmt_ip_mode", None)
    if mgmt_ip_mode is None or (
        hasattr(mgmt_ip_mode, "value") and mgmt_ip_mode.value == "inactive"
    ):
        return True, "Management IP mode is inactive", {}

    # Build config dict from assignment
    config: dict[str, str] = {}
    mode_value = mgmt_ip_mode.value if hasattr(mgmt_ip_mode, "value") else str(mgmt_ip_mode)
    config["IP Mode"] = mode_value.upper()

    if mode_value == "static_ip":
        ip_address = getattr(active_assignment, "mgmt_ip_address", None)
        if ip_address:
            config["IP Address"] = ip_address

    # Get VLAN from assignment or OLT config pack
    mgmt_vlan = getattr(active_assignment, "mgmt_vlan", None)
    if mgmt_vlan:
        config["VLAN"] = str(mgmt_vlan.tag)

    return True, f"Management IP configured ({mode_value})", config


def running_config_context(db: Session, ont_id: str) -> dict[str, object]:
    """Build display context for an ONT ACS running-config read."""
    result = fetch_running_config(db, ont_id)
    labels = {
        "device_info": "Device Info",
        "wan": "WAN / IP",
        "optical": "Optical",
        "wifi": "WiFi",
    }
    sections: list[dict[str, object]] = []
    for key, label in labels.items():
        values = (result.data or {}).get(key) if result.success else None
        if not isinstance(values, dict):
            continue
        rows = [
            {"key": row_key, "value": row_value}
            for row_key, row_value in values.items()
            if row_value is not None and str(row_value).strip() != ""
        ]
        if rows:
            sections.append({"key": key, "label": label, "rows": rows})
    return {
        "ont_id": ont_id,
        "config_result": result,
        "config_sections": sections,
    }
