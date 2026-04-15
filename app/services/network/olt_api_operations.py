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
from app.services.network.action_logging import actor_label
from app.services.network.olt import OLTDevices
from app.services.network.olt_authorization_workflow import (
    AuthorizationWorkflowResult,
    authorize_autofind_ont_and_provision_network_audited,
    queue_authorize_autofind_ont,
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


def queue_authorize_ont(
    db: Session,
    olt_id: str,
    *,
    fsp: str,
    serial_number: str,
    force_reauthorize: bool = False,
    request: Request | None = None,
) -> OltApiWriteResult:
    ok, message, operation_id = queue_authorize_autofind_ont(
        db,
        olt_id=olt_id,
        fsp=fsp,
        serial_number=serial_number,
        force_reauthorize=force_reauthorize,
        initiated_by=actor_label(request),
        request=request,
    )
    return OltApiWriteResult(
        ok,
        message,
        {
            "status": "queued" if ok else "error",
            "operation_id": operation_id,
            "olt_id": olt_id,
            "fsp": fsp,
            "serial_number": serial_number,
            "force_reauthorize": force_reauthorize,
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
    from app.services.network.olt_ssh_service_ports import create_single_service_port
    from app.services.network.olt_write_reconciliation import (
        verify_service_port_present,
    )

    olt = load_olt(db, olt_id)
    ok, message = create_single_service_port(
        olt,
        fsp,
        ont_id,
        gem_index,
        vlan_id,
        user_vlan=user_vlan,
        tag_transform=tag_transform,
    )
    if not ok:
        return OltApiWriteResult(False, message)

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
    from app.services.network.olt_ssh_service_ports import (
        delete_service_port as ssh_delete_service_port,
    )
    from app.services.network.olt_write_reconciliation import (
        verify_service_port_index_absent,
    )

    olt = load_olt(db, olt_id)
    ok, message = ssh_delete_service_port(olt, index)
    if not ok:
        return OltApiWriteResult(False, message)

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
    from app.services.network.olt_ssh import create_tr069_server_profile
    from app.services.network.olt_ssh_profiles import get_tr069_server_profiles

    olt = load_olt(db, olt_id)
    ok, message = create_tr069_server_profile(
        olt,
        profile_name=profile_name,
        acs_url=acs_url,
        username=username,
        password=password,
        inform_interval=inform_interval,
    )
    if not ok:
        return OltApiWriteResult(False, message)

    read_ok, read_msg, profiles = get_tr069_server_profiles(olt)
    if not read_ok:
        return OltApiWriteResult(
            False,
            f"OLT accepted the TR-069 profile write, but readback failed: {read_msg}",
        )
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
