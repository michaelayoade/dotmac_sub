"""Service-layer facades for OLT operational API writes.

Routers should only load inputs and shape responses. OLT writes live here so
API and web paths can share audited workflows and readback reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.models.network import OLTDevice
from app.services.network import olt_operations
from app.services.network.olt import OLTDevices
from app.services.network.olt_authorization_workflow import (
    AuthorizationWorkflowResult,
    authorize_autofind_ont_and_provision_network_audited,
)


@dataclass(frozen=True)
class OltApiWriteResult:
    success: bool
    message: str
    data: dict[str, object] | None = None


def load_olt(db: Session, olt_id: str) -> OLTDevice:
    return OLTDevices.get(db, olt_id)  # type: ignore[return-value]


def authorize_ont(
    db: Session,
    olt_id: str,
    *,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
    request: Request | None = None,
) -> AuthorizationWorkflowResult:
    return authorize_autofind_ont_and_provision_network_audited(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        request=request,
    )


def _serialize_autofind_entry(entry: object) -> dict[str, object]:
    return {
        "fsp": getattr(entry, "fsp", ""),
        "serial_number": getattr(entry, "serial_number", ""),
        "serial_hex": getattr(entry, "serial_hex", None),
        "vendor_id": getattr(entry, "vendor_id", None),
        "model": getattr(entry, "model", None),
        "software_version": getattr(entry, "software_version", None),
        "mac": getattr(entry, "mac", None),
    }


def _serialize_service_port(entry: object) -> dict[str, object]:
    return {
        "index": getattr(entry, "index", None),
        "vlan_id": getattr(entry, "vlan_id", None),
        "ont_id": getattr(entry, "ont_id", None),
        "gem_index": getattr(entry, "gem_index", None),
        "flow_type": getattr(entry, "flow_type", None),
        "state": getattr(entry, "state", None),
    }


def _serialize_profile(entry: object) -> dict[str, object]:
    return {
        "profile_id": getattr(entry, "profile_id", None),
        "name": getattr(entry, "name", None),
    }


def _serialize_tr069_profile(entry: object) -> dict[str, object]:
    return {
        "profile_id": getattr(entry, "profile_id", None),
        "name": getattr(entry, "name", None),
        "acs_url": getattr(entry, "acs_url", None),
        "username": getattr(entry, "acs_username", None),
    }


def discover_onts(db: Session, olt_id: str) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).get_autofind_onts()
    entries = result.data.get("autofind_entries", [])
    data = [_serialize_autofind_entry(entry) for entry in entries]
    return OltApiWriteResult(result.success, result.message, {"entries": data})


def list_service_ports(db: Session, olt_id: str, *, fsp: str) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).get_service_ports(fsp)
    entries = result.data.get("service_ports", [])
    data = [_serialize_service_port(entry) for entry in entries]
    return OltApiWriteResult(result.success, result.message, {"entries": data})


def list_service_ports_for_ont(
    db: Session,
    olt_id: str,
    *,
    fsp: str,
    ont_id: int,
) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).get_service_ports_for_ont(fsp, ont_id)
    entries = result.data.get("service_ports", [])
    data = [_serialize_service_port(entry) for entry in entries]
    return OltApiWriteResult(result.success, result.message, {"entries": data})


def get_line_profiles(db: Session, olt_id: str) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).get_line_profiles()
    entries = (result.data or {}).get("profiles", [])
    return OltApiWriteResult(
        result.success,
        result.message,
        {"entries": [_serialize_profile(entry) for entry in entries]},
    )


def get_service_profiles(db: Session, olt_id: str) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).get_service_profiles()
    entries = (result.data or {}).get("profiles", [])
    return OltApiWriteResult(
        result.success,
        result.message,
        {"entries": [_serialize_profile(entry) for entry in entries]},
    )


def get_tr069_profiles(db: Session, olt_id: str) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).get_tr069_profiles()
    entries = (result.data or {}).get("profiles", [])
    return OltApiWriteResult(
        result.success,
        result.message,
        {"entries": [_serialize_tr069_profile(entry) for entry in entries]},
    )


def authorize_ont_resilient(
    db: Session,
    olt_id: str,
    *,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
    request: Request | None = None,
    prefer_sync: bool = False,
) -> OltApiWriteResult:
    """Authorize ONT with async/sync fallback for resilience.

    Tries Celery (async) first, falls back to synchronous execution
    if Celery/Redis is unavailable.

    Args:
        db: Database session
        olt_id: OLT device ID
        fsp: Frame/Slot/Port location
        serial_number: ONT serial number
        force_reauthorize: Delete existing registration first
        request: HTTP request for audit logging
        prefer_sync: Skip async and run synchronously

    Returns:
        OltApiWriteResult with execution details
    """
    from app.services.network.authorization_executor import execute_authorization

    result = execute_authorization(
        db,
        olt_id,
        fsp,
        serial_number,
        force_reauthorize=force_reauthorize,
        request=request,
        prefer_sync=prefer_sync,
    )

    status = "queued" if result.mode.value == "async_queued" else result.mode.value
    if not result.success:
        status = "error"

    return OltApiWriteResult(
        result.success,
        result.message,
        {
            "status": status,
            "mode": result.mode.value,
            "operation_id": result.operation_id,
            "ont_id": result.ont_id,
            "olt_id": olt_id,
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
            **result.details,
        },
    )


def create_service_port(
    db: Session,
    olt_id: str,
    *,
    fsp: str,
    ont_id: int,
    gem_index: int,
    vlan_id: int,
    user_vlan: int | None = None,
    tag_transform: str = "translate",
) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.olt_write_reconciliation import (
        verify_service_port_present,
    )

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).create_service_port(
        fsp,
        ont_id,
        gem_index=gem_index,
        vlan_id=vlan_id,
        user_vlan=user_vlan,
        tag_transform=tag_transform,
    )
    if not result.success:
        return OltApiWriteResult(False, result.message)

    verification = verify_service_port_present(
        olt,
        fsp=fsp,
        ont_id=ont_id,
        vlan_id=vlan_id,
        gem_index=gem_index,
    )
    if not verification.success:
        return OltApiWriteResult(False, verification.message, verification.details)
    return OltApiWriteResult(True, verification.message, verification.details)


def delete_service_port(
    db: Session,
    olt_id: str,
    *,
    index: int,
) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter
    from app.services.network.olt_write_reconciliation import (
        verify_service_port_index_absent,
    )

    olt = load_olt(db, olt_id)
    result = get_protocol_adapter(olt).delete_service_port(index)
    if not result.success:
        return OltApiWriteResult(False, result.message)

    verification = verify_service_port_index_absent(olt, service_port_index=index)
    if not verification.success:
        return OltApiWriteResult(False, verification.message, verification.details)
    return OltApiWriteResult(True, verification.message, verification.details)


def create_tr069_profile(
    db: Session,
    olt_id: str,
    *,
    profile_name: str,
    acs_url: str,
    username: str,
    password: str,
    inform_interval: int,
) -> OltApiWriteResult:
    from app.services.network.olt_protocol_adapters import get_protocol_adapter

    olt = load_olt(db, olt_id)
    adapter = get_protocol_adapter(olt)
    result = adapter.create_tr069_profile(
        profile_name=profile_name,
        acs_url=acs_url,
        username=username,
        password=password,
        inform_interval=inform_interval,
    )
    if not result.success:
        return OltApiWriteResult(False, result.message)

    readback = adapter.get_tr069_profiles()
    if not readback.success:
        return OltApiWriteResult(
            False,
            f"OLT accepted the TR-069 profile write, but readback failed: {readback.message}",
        )
    profiles = (readback.data or {}).get("profiles", [])
    for profile in profiles:
        if getattr(profile, "name", None) != profile_name:
            continue
        observed_url = str(getattr(profile, "acs_url", "") or "")
        if observed_url and observed_url != acs_url:
            continue
        return OltApiWriteResult(
            True,
            f"Verified TR-069 profile {profile_name} on the OLT.",
            {
                "profile_id": getattr(profile, "profile_id", None),
                "name": profile_name,
                "acs_url": observed_url or acs_url,
            },
        )
    return OltApiWriteResult(
        False,
        "OLT accepted the TR-069 profile write, but the profile was not present on readback.",
        {"name": profile_name, "acs_url": acs_url},
    )


def run_config_backup(db: Session, olt_id: str) -> OltApiWriteResult:
    backup, message = olt_operations.backup_running_config_ssh(db, olt_id)
    if backup is None:
        return OltApiWriteResult(False, message)
    return OltApiWriteResult(
        True,
        message,
        {
            "backup_id": str(backup.id),
            "file_path": backup.file_path,
            "file_size_bytes": backup.file_size_bytes,
            "file_hash": backup.file_hash,
        },
    )


def test_ssh_connection(db: Session, olt_id: str, *, request: Request | None = None):
    return olt_operations.test_olt_ssh_connection(db, olt_id, request=request)


def run_cli_command(
    db: Session,
    olt_id: str,
    *,
    command: str,
    request: Request | None = None,
) -> OltApiWriteResult:
    ok, message, output = olt_operations.execute_cli_command(
        db, olt_id, command, request=request
    )
    return OltApiWriteResult(ok, message, {"output": output})
