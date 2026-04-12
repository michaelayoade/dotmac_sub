"""OLT operational API — SSH discovery, authorization, service-ports, profiles, CLI."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.network import OLTDevice
from app.schemas.network_olt_ops import (
    OltAuthorizeOntRequest,
    OltCliCommandRequest,
    OltDiscoveredOntRead,
    OltOperationResponse,
    OltProfileRead,
    OltServicePortCreateRequest,
    OltServicePortRead,
    OltTr069ProfileCreateRequest,
    OltTr069ProfileRead,
)
from app.services.auth_dependencies import require_permission
from app.services.network.olt import OLTDevices
from app.services.network import olt_api_operations

logger = logging.getLogger(__name__)

router = APIRouter(tags=["network-olt-operations"])


def _load_olt(db: Session, olt_id: str) -> OLTDevice:
    """Load OLTDevice or raise 404."""
    return OLTDevices.get(db, olt_id)  # type: ignore[return-value]


# ── ONT Discovery & Authorization ─────────────────────────────────────


@router.post(
    "/olt-devices/{olt_id}/discover-onts",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def discover_onts(olt_id: str, db: Session = Depends(get_db)) -> OltOperationResponse:
    from app.services.network.olt_ssh import get_autofind_onts

    olt = _load_olt(db, olt_id)
    success, message, entries = get_autofind_onts(olt)
    if not success:
        raise HTTPException(status_code=422, detail=message)
    data = [
        OltDiscoveredOntRead(
            fsp=e.fsp,
            serial_number=e.serial_number,
            serial_hex=e.serial_hex,
            vendor_id=e.vendor_id,
            model=e.model,
            software_version=e.software_version,
            mac=e.mac,
        ).model_dump()
        for e in entries
    ]
    return OltOperationResponse(success=True, message=message, data=data)


@router.post(
    "/olt-devices/{olt_id}/authorize-ont",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def authorize_ont(
    request: Request,
    olt_id: str,
    payload: OltAuthorizeOntRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.authorize_ont(
        db,
        olt_id,
        fsp=payload.fsp,
        serial_number=payload.serial_number,
        force_reauthorize=payload.force_reauthorize,
        request=request,
    )
    if not result.success and result.status != "warning":
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(
        success=result.success,
        message=result.message,
        data=result.to_dict(),
    )


# ── Service Ports ──────────────────────────────────────────────────────


@router.get(
    "/olt-devices/{olt_id}/service-ports",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def list_service_ports(
    olt_id: str,
    fsp: str = Query(description="Frame/Slot/Port e.g. 0/1/0"),
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    from app.services.network.olt_ssh import get_service_ports

    olt = _load_olt(db, olt_id)
    success, message, entries = get_service_ports(olt, fsp)
    if not success:
        raise HTTPException(status_code=422, detail=message)
    data = [
        OltServicePortRead(
            index=e.index,
            vlan_id=e.vlan_id,
            ont_id=e.ont_id,
            gem_index=e.gem_index,
            flow_type=e.flow_type,
            state=e.state,
        ).model_dump()
        for e in entries
    ]
    return OltOperationResponse(success=True, message=message, data=data)


@router.post(
    "/olt-devices/{olt_id}/service-ports",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def create_service_port(
    olt_id: str,
    payload: OltServicePortCreateRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.create_service_port(
        db,
        olt_id,
        fsp=payload.fsp,
        ont_id=payload.ont_id,
        gem_index=payload.gem_index,
        vlan_id=payload.vlan_id,
        user_vlan=payload.user_vlan,
        tag_transform=payload.tag_transform,
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


@router.delete(
    "/olt-devices/{olt_id}/service-ports/{index}",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def delete_service_port(
    olt_id: str, index: int, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.delete_service_port(db, olt_id, index=index)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


@router.get(
    "/olt-devices/{olt_id}/service-ports/ont/{ont_id}",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_service_ports_for_ont(
    olt_id: str,
    ont_id: int,
    fsp: str = Query(description="Frame/Slot/Port e.g. 0/1/0"),
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    from app.services.network.olt_ssh_service_ports import (
        get_service_ports_for_ont as ssh_get_for_ont,
    )

    olt = _load_olt(db, olt_id)
    success, message, entries = ssh_get_for_ont(olt, fsp, ont_id)
    if not success:
        raise HTTPException(status_code=422, detail=message)
    data = [
        OltServicePortRead(
            index=e.index,  # type: ignore[attr-defined]
            vlan_id=e.vlan_id,  # type: ignore[attr-defined]
            ont_id=e.ont_id,  # type: ignore[attr-defined]
            gem_index=e.gem_index,  # type: ignore[attr-defined]
            flow_type=e.flow_type,  # type: ignore[attr-defined]
            state=e.state,  # type: ignore[attr-defined]
        ).model_dump()
        for e in entries
    ]
    return OltOperationResponse(success=True, message=message, data=data)


# ── Profiles ───────────────────────────────────────────────────────────


@router.get(
    "/olt-devices/{olt_id}/profiles/line",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_line_profiles(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    from app.services.network.olt_ssh_profiles import get_line_profiles as ssh_get

    olt = _load_olt(db, olt_id)
    success, message, entries = ssh_get(olt)
    if not success:
        raise HTTPException(status_code=422, detail=message)
    data = [
        OltProfileRead(profile_id=e.profile_id, name=e.name).model_dump()  # type: ignore[attr-defined]
        for e in entries
    ]
    return OltOperationResponse(success=True, message=message, data=data)


@router.get(
    "/olt-devices/{olt_id}/profiles/service",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_service_profiles(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    from app.services.network.olt_ssh_profiles import get_service_profiles as ssh_get

    olt = _load_olt(db, olt_id)
    success, message, entries = ssh_get(olt)
    if not success:
        raise HTTPException(status_code=422, detail=message)
    data = [
        OltProfileRead(profile_id=e.profile_id, name=e.name).model_dump()  # type: ignore[attr-defined]
        for e in entries
    ]
    return OltOperationResponse(success=True, message=message, data=data)


@router.get(
    "/olt-devices/{olt_id}/profiles/tr069",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_tr069_profiles(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    from app.services.network.olt_ssh_profiles import (
        get_tr069_server_profiles as ssh_get,
    )

    olt = _load_olt(db, olt_id)
    success, message, entries = ssh_get(olt)
    if not success:
        raise HTTPException(status_code=422, detail=message)
    data = [
        OltTr069ProfileRead(
            profile_id=e.profile_id,
            name=e.name,
            acs_url=getattr(e, "acs_url", None),
            username=getattr(e, "acs_username", None),
        ).model_dump()
        for e in entries
    ]
    return OltOperationResponse(success=True, message=message, data=data)


@router.post(
    "/olt-devices/{olt_id}/profiles/tr069",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def create_tr069_profile(
    olt_id: str,
    payload: OltTr069ProfileCreateRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    result = olt_api_operations.create_tr069_profile(
        db,
        olt_id,
        profile_name=payload.name,
        acs_url=payload.acs_url,
        username=payload.username,
        password=payload.password,
        inform_interval=payload.inform_interval,
    )
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


# ── Config & Connectivity ─────────────────────────────────────────────


@router.post(
    "/olt-devices/{olt_id}/config-backup",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def fetch_config_backup(
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    result = olt_api_operations.run_config_backup(db, olt_id)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OltOperationResponse(success=True, message=result.message, data=result.data)


@router.post(
    "/olt-devices/{olt_id}/test-connection",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def test_olt_connection(
    request: Request,
    olt_id: str, db: Session = Depends(get_db)
) -> OltOperationResponse:
    success, message, policy_key = olt_api_operations.test_ssh_connection(
        db, olt_id, request=request
    )
    if not success:
        raise HTTPException(status_code=422, detail=message)
    return OltOperationResponse(
        success=True,
        message=message,
        data={"version": policy_key, "policy_key": policy_key},
    )


@router.post(
    "/olt-devices/{olt_id}/cli-command",
    response_model=OltOperationResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def run_cli_command(
    request: Request,
    olt_id: str,
    payload: OltCliCommandRequest,
    db: Session = Depends(get_db),
) -> OltOperationResponse:
    from app.services.network.olt_operations import execute_cli_command

    success, message, output = execute_cli_command(
        db, olt_id, payload.command, request=request
    )
    if not success:
        raise HTTPException(status_code=422, detail=message)
    return OltOperationResponse(success=True, message=message, data={"output": output})
