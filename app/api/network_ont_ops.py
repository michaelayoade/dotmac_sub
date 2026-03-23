"""ONT operational API — actions, enriched reads, writes, features, bulk ops."""

from __future__ import annotations

import logging
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.network_ont_ops import (
    OntActionResponse,
    OntBulkActionRequest,
    OntBulkActionResponse,
    OntBulkActionStatus,
    OntConnectionRequestCredentials,
    OntEnrichedRead,
    OntExternalIdUpdate,
    OntFeatureToggleRequest,
    OntFirmwareRequest,
    OntLanPortToggleRequest,
    OntMaxMacLearnRequest,
    OntMgmtIpUpdate,
    OntMoveRequest,
    OntPingRequest,
    OntPPPoERequest,
    OntProvisionRequest,
    OntProvisionResponse,
    OntServicePortUpdate,
    OntSpeedProfileUpdate,
    OntTracerouteRequest,
    OntWanConfigUpdate,
    OntWebCredentialsRequest,
    OntWifiConfigRequest,
    OntWifiPasswordRequest,
    OntWifiSsidRequest,
)
from app.services.auth_dependencies import require_permission
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_actions import ont_actions
from app.services.network.ont_provisioning_orchestrator import (
    OntProvisioningOrchestrator,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["network-ont-operations"])


# ── Helper ─────────────────────────────────────────────────────────────


def _action_response(result: ActionResult) -> OntActionResponse:
    """Convert an ActionResult dataclass to the API response schema."""
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    return OntActionResponse(
        success=result.success,
        message=result.message,
        data=result.data,
    )


# ── Phase 1: ONT Remote Actions ───────────────────────────────────────


@router.post(
    "/ont-units/{ont_id}/reboot",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def reboot_ont(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.reboot(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/factory-reset",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def factory_reset_ont(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.factory_reset(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/refresh-status",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def refresh_ont_status(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.refresh_status(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/running-config",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_running_config(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.get_running_config(db, ont_id)
    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)
    # DeviceConfig → dict for data field
    config_data = result.data
    if hasattr(result, "data") and result.data is None:
        # get_running_config returns ActionResult with DeviceConfig in data
        config_data = None
    return OntActionResponse(success=True, message=result.message, data=config_data)


@router.post(
    "/ont-units/{ont_id}/wifi/ssid",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def set_wifi_ssid(
    ont_id: str, payload: OntWifiSsidRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.set_wifi_ssid(db, ont_id, payload.ssid)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/wifi/password",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def set_wifi_password(
    ont_id: str, payload: OntWifiPasswordRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.set_wifi_password(db, ont_id, payload.password)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/lan-ports/{port}/toggle",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def toggle_lan_port(
    ont_id: str,
    port: int,
    payload: OntLanPortToggleRequest,
    db: Session = Depends(get_db),
) -> OntActionResponse:
    result = ont_actions.toggle_lan_port(db, ont_id, port, payload.enabled)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/pppoe",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def set_pppoe_credentials(
    ont_id: str, payload: OntPPPoERequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.set_pppoe_credentials(
        db, ont_id, payload.username, payload.password
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/enable-ipv6",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def enable_ipv6_on_ont(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.enable_ipv6_on_wan(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/connection-request",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def send_connection_request(
    ont_id: str, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.send_connection_request(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/connection-request-credentials",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def set_connection_request_credentials(
    ont_id: str, payload: OntConnectionRequestCredentials, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.set_connection_request_credentials(
        db,
        ont_id,
        payload.username,
        payload.password,
        periodic_inform_interval=payload.periodic_inform_interval,
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/diagnostics/ping",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def run_ping_diagnostic(
    ont_id: str, payload: OntPingRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.run_ping_diagnostic(db, ont_id, payload.target, payload.count)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/diagnostics/traceroute",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:read"))],
)
def run_traceroute_diagnostic(
    ont_id: str, payload: OntTracerouteRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.run_traceroute_diagnostic(db, ont_id, payload.target)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/firmware-upgrade",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def firmware_upgrade(
    ont_id: str, payload: OntFirmwareRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.firmware_upgrade(db, ont_id, payload.firmware_image_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/provision",
    response_model=OntProvisionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def provision_ont(
    ont_id: str, payload: OntProvisionRequest, db: Session = Depends(get_db)
) -> OntProvisionResponse:
    if payload.async_mode and not payload.dry_run:
        from app.tasks.ont_provisioning import provision_ont_async

        task = provision_ont_async.delay(
            ont_id,
            payload.profile_id,
            tr069_olt_profile_id=payload.tr069_olt_profile_id,
        )
        return OntProvisionResponse(
            success=True,
            message="Provisioning job queued",
            task_id=str(task.id),
        )

    job = OntProvisioningOrchestrator.provision_ont(
        db,
        ont_id,
        payload.profile_id,
        dry_run=payload.dry_run,
        tr069_olt_profile_id=payload.tr069_olt_profile_id,
    )
    result_dict = job.to_dict()
    if not job.success and not job.dry_run:
        raise HTTPException(status_code=422, detail=result_dict)
    return OntProvisionResponse(
        success=job.success,
        message=job.message,
        steps=result_dict.get("steps", []),
        commands_preview=result_dict.get("command_sets", []),
        dry_run=job.dry_run,
    )


# ── Phase 4: ONT Enriched Reads ───────────────────────────────────────


@router.get(
    "/ont-units/{ont_id}/enriched",
    response_model=OntEnrichedRead,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_enriched_ont(
    ont_id: str,
    live: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_enriched(db, ont_id, live_query=live)


@router.get(
    "/ont-units/{ont_id}/capabilities",
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_capabilities(ont_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_capabilities(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/tr069-summary",
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_tr069_summary(ont_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_tr069_summary(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/lan-hosts",
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_lan_hosts(
    ont_id: str, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_lan_hosts(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/ethernet-ports",
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_ethernet_ports(
    ont_id: str, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_ethernet_ports(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/vlan-chain",
    dependencies=[Depends(require_permission("network:read"))],
)
def get_ont_vlan_chain(ont_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_vlan_chain_status(db, ont_id)


# ── Phase 5: ONT Write Operations ─────────────────────────────────────


@router.put(
    "/ont-units/{ont_id}/speed-profile",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_speed_profile(
    ont_id: str, payload: OntSpeedProfileUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.update_speed_profile(
        db,
        ont_id,
        download_profile_id=payload.download_profile_id,
        upload_profile_id=payload.upload_profile_id,
    )
    return _action_response(result)


@router.put(
    "/ont-units/{ont_id}/wan-config",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_wan_config(
    ont_id: str, payload: OntWanConfigUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.update_wan_config(
        db,
        ont_id,
        wan_mode=payload.wan_mode,
        vlan_id=payload.vlan_id,
        pppoe_username=payload.pppoe_username,
        pppoe_password=payload.pppoe_password,
    )
    return _action_response(result)


@router.put(
    "/ont-units/{ont_id}/management-ip",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_management_ip(
    ont_id: str, payload: OntMgmtIpUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.update_management_ip(
        db,
        ont_id,
        mgmt_ip_mode=payload.mgmt_ip_mode,
        mgmt_vlan_id=payload.mgmt_vlan_id,
        mgmt_ip_address=payload.mgmt_ip_address,
        mgmt_subnet=payload.mgmt_subnet,
        mgmt_gateway=payload.mgmt_gateway,
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/service-port",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_service_port(
    ont_id: str, payload: OntServicePortUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.update_service_port(
        db,
        ont_id,
        vlan_id=payload.vlan_id,
        gem_index=payload.gem_index,
        user_vlan=payload.user_vlan,
        tag_transform=payload.tag_transform,
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/move",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def move_ont(
    ont_id: str, payload: OntMoveRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.move_ont(
        db, ont_id, target_pon_port_id=payload.target_pon_port_id
    )
    return _action_response(result)


@router.patch(
    "/ont-units/{ont_id}/external-id",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_external_id(
    ont_id: str, payload: OntExternalIdUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.update_external_id(db, ont_id, external_id=payload.external_id)
    return _action_response(result)


# ── Phase 6: ONT Feature Toggles ──────────────────────────────────────


@router.post(
    "/ont-units/{ont_id}/features/wifi",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def set_ont_wifi_config(
    ont_id: str, payload: OntWifiConfigRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.set_wifi_config(
        db,
        ont_id,
        ssid=payload.ssid,
        password=payload.password,
        enabled=payload.enabled,
        band=payload.band,
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/voip",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def toggle_ont_voip(
    ont_id: str, payload: OntFeatureToggleRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.toggle_voip(db, ont_id, enabled=payload.enabled)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/catv",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def toggle_ont_catv(
    ont_id: str, payload: OntFeatureToggleRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.toggle_catv(db, ont_id, enabled=payload.enabled)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/iptv",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def toggle_ont_iptv(
    ont_id: str, payload: OntFeatureToggleRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.toggle_iptv(db, ont_id, enabled=payload.enabled)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/wan-remote-access",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def toggle_ont_wan_remote_access(
    ont_id: str, payload: OntFeatureToggleRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.toggle_wan_remote_access(db, ont_id, enabled=payload.enabled)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/lan-port/{port}",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def toggle_ont_lan_port_feature(
    ont_id: str,
    port: int,
    payload: OntFeatureToggleRequest,
    db: Session = Depends(get_db),
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.toggle_lan_port(
        db, ont_id, port_number=port, enabled=payload.enabled
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/dhcp-snooping",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def configure_ont_dhcp_snooping(
    ont_id: str, payload: OntFeatureToggleRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.configure_dhcp_snooping(db, ont_id, enabled=payload.enabled)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/max-mac-learn",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def set_ont_max_mac_learn(
    ont_id: str, payload: OntMaxMacLearnRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.set_max_mac_learn(db, ont_id, max_mac=payload.max_mac)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/web-credentials",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def update_ont_web_credentials(
    ont_id: str, payload: OntWebCredentialsRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.update_web_credentials(
        db, ont_id, username=payload.username, password=payload.password
    )
    return _action_response(result)


# ── Phase 7: Bulk Operations ──────────────────────────────────────────


ALLOWED_BULK_ACTIONS = {
    "reboot",
    "factory_reset",
    "speed_update",
    "catv_toggle",
    "wifi_update",
    "voip_toggle",
}


@router.post(
    "/ont-units/bulk-action",
    response_model=OntBulkActionResponse,
    dependencies=[Depends(require_permission("network:write"))],
)
def submit_bulk_action(payload: OntBulkActionRequest) -> OntBulkActionResponse:
    if payload.action not in ALLOWED_BULK_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{payload.action}'. Allowed: {sorted(ALLOWED_BULK_ACTIONS)}",
        )
    from app.celery_app import celery_app

    task = celery_app.send_task(
        "app.tasks.ont_bulk.execute_bulk_action",
        args=[payload.ont_ids, payload.action, payload.params],
    )
    return OntBulkActionResponse(
        task_id=task.id,
        message=f"Bulk {payload.action} queued for {len(payload.ont_ids)} ONT(s)",
    )


@router.get(
    "/ont-units/bulk-action/{task_id}",
    response_model=OntBulkActionStatus,
    dependencies=[Depends(require_permission("network:read"))],
)
def get_bulk_action_status(task_id: str) -> OntBulkActionStatus:
    result = AsyncResult(task_id)
    return OntBulkActionStatus(
        task_id=task_id,
        status=result.status,
        result=result.result
        if result.ready() and isinstance(result.result, dict)
        else None,
    )
