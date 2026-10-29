"""ONT operational API — actions, enriched reads, writes, features, bulk ops."""

from __future__ import annotations

import logging
from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.network_ont_ops import (
    BulkProvisioningRunRead,
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
    OntServicePortUpdate,
    OntSpeedProfileUpdate,
    OntTracerouteRequest,
    OntWebCredentialsRequest,
    OntWifiConfigRequest,
    OntWifiPasswordRequest,
    OntWifiSsidRequest,
)
from app.services.auth_dependencies import require_permission
from app.services.network.ont_action_common import ActionResult
from app.services.network.ont_actions import ont_actions

logger = logging.getLogger(__name__)


def _retired_writer(what: str, guidance: str) -> HTTPException:
    return HTTPException(
        status_code=410,
        detail=(
            f"{what} through this API is retired: the ONT configuration "
            f"lifecycle is the only writer of ONT/OLT/ACS state. {guidance}"
        ),
    )


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


# ── ONT remote actions ────────────────────────────────────────────────


@router.post(
    "/ont-units/{ont_id}/reboot",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def reboot_ont(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.reboot(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/factory-reset",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def factory_reset_ont(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.factory_reset(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/refresh-status",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def refresh_ont_status(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.refresh_status(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/running-config",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_running_config(ont_id: str, db: Session = Depends(get_db)) -> OntActionResponse:
    result = ont_actions.get_running_config(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/wifi/ssid",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def set_wifi_ssid(
    ont_id: str,
    payload: OntWifiSsidRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OntActionResponse:
    from app.services.web_network_ont_actions.lifecycle_admission import (
        submit_wifi_change,
    )

    result = submit_wifi_change(db, request, ont_id, ssid=payload.ssid)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/wifi/password",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def set_wifi_password(
    ont_id: str,
    payload: OntWifiPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OntActionResponse:
    from app.services.web_network_ont_actions.lifecycle_admission import (
        submit_wifi_change,
    )

    result = submit_wifi_change(db, request, ont_id, password=payload.password)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/lan-ports/{port}/toggle",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    "/ont-units/{ont_id}/connection-request",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def send_connection_request(
    ont_id: str, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.send_connection_request(db, ont_id)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/connection-request-credentials",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def run_ping_diagnostic(
    ont_id: str, payload: OntPingRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.run_ping_diagnostic(db, ont_id, payload.target, payload.count)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/diagnostics/traceroute",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def run_traceroute_diagnostic(
    ont_id: str, payload: OntTracerouteRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.run_traceroute_diagnostic(db, ont_id, payload.target)
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/firmware-upgrade",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def firmware_upgrade(
    ont_id: str, payload: OntFirmwareRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    result = ont_actions.firmware_upgrade(db, ont_id, payload.firmware_image_id)
    return _action_response(result)


# ── ONT enriched reads ────────────────────────────────────────────────


@router.get(
    "/ont-units/{ont_id}/enriched",
    response_model=OntEnrichedRead,
    dependencies=[Depends(require_permission("network:ont:read"))],
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
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_ont_capabilities(ont_id: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_capabilities(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/tr069-summary",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_ont_tr069_summary(ont_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_tr069_summary(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/lan-hosts",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_ont_lan_hosts(
    ont_id: str, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_lan_hosts(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/ethernet-ports",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_ont_ethernet_ports(
    ont_id: str, db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_ethernet_ports(db, ont_id)


@router.get(
    "/ont-units/{ont_id}/vlan-chain",
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_ont_vlan_chain(ont_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.services.network.ont_read import ont_read

    return ont_read.get_vlan_chain_status(db, ont_id)


# ── ONT write operations ──────────────────────────────────────────────


@router.put(
    "/ont-units/{ont_id}/speed-profile",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    "/ont-units/{ont_id}/management-ip",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def update_ont_management_ip(
    ont_id: str, payload: OntMgmtIpUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    """Retired: wrote the IPHOST directly to the OLT, bypassing the lifecycle."""
    raise _retired_writer(
        "Management IP updates",
        "Use the admin Configure tab's Management section.",
    )


@router.post(
    "/ont-units/{ont_id}/service-port",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def update_ont_service_port(
    ont_id: str, payload: OntServicePortUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    """Retired: created OLT service ports outside the lifecycle."""
    raise _retired_writer(
        "Service port creation",
        "Service ports are planned by the ONT configuration lifecycle; "
        "use the Configure tab's WAN or Management section.",
    )


@router.post(
    "/ont-units/{ont_id}/move",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def move_ont(
    ont_id: str, payload: OntMoveRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    """Retired: moved the ONT without deleting its old service ports."""
    raise _retired_writer(
        "ONT move",
        "Raise the move for manual handling until the lifecycle owns it.",
    )


@router.patch(
    "/ont-units/{ont_id}/external-id",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def update_ont_external_id(
    ont_id: str, payload: OntExternalIdUpdate, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_write import ont_write

    result = ont_write.update_external_id(db, ont_id, external_id=payload.external_id)
    return _action_response(result)


# ── ONT feature toggles ───────────────────────────────────────────────


@router.post(
    "/ont-units/{ont_id}/features/wifi",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def set_ont_wifi_config(
    ont_id: str,
    payload: OntWifiConfigRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OntActionResponse:
    """Queue a sparse WiFi change through the ONT configuration lifecycle."""
    from app.services.web_network_ont_actions.lifecycle_admission import (
        submit_wifi_change,
    )

    if payload.band:
        raise HTTPException(
            status_code=422,
            detail="Per-band WiFi changes are not supported by the lifecycle.",
        )
    result = submit_wifi_change(
        db,
        request,
        ont_id,
        ssid=payload.ssid,
        password=payload.password,
        enabled=payload.enabled,
        channel=None if payload.channel is None else str(payload.channel),
        security_mode=payload.security_mode,
    )
    return _action_response(result)


@router.post(
    "/ont-units/{ont_id}/features/voip",
    response_model=OntActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
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
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def update_ont_web_credentials(
    ont_id: str, payload: OntWebCredentialsRequest, db: Session = Depends(get_db)
) -> OntActionResponse:
    from app.services.network.ont_features import ont_features

    result = ont_features.update_web_credentials(
        db, ont_id, username=payload.username, password=payload.password
    )
    return _action_response(result)


# ── Bulk ONT operations ───────────────────────────────────────────────


ALLOWED_BULK_ACTIONS = {
    "reboot",
    "factory_reset",
    "speed_update",
    "catv_toggle",
    "wifi_update",
    "voip_toggle",
    "provision",
}


@router.post(
    "/ont-units/bulk-action",
    response_model=OntBulkActionResponse,
    dependencies=[Depends(require_permission("network:ont:write"))],
)
def submit_bulk_action(
    payload: OntBulkActionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> OntBulkActionResponse:
    if payload.action not in ALLOWED_BULK_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{payload.action}'. Allowed: {sorted(ALLOWED_BULK_ACTIONS)}",
        )
    if payload.action == "provision":
        from app.services.network.bulk_provisioning import bulk_provision_onts

        params = dict(payload.params or {})
        try:
            result = bulk_provision_onts(
                db,
                payload.ont_ids,
                max_workers=int(params.get("max_parallel") or 10),
                chunk_delay_seconds=int(params.get("chunk_delay_seconds") or 15),
                initiated_by=params.get("initiated_by"),
                correlation_key=params.get("correlation_key"),
                dry_run=bool(params.get("dry_run", False)),
                allow_low_optical_margin=bool(
                    params.get("allow_low_optical_margin", False)
                ),
                step_data=dict(params.get("step_data") or {}),
                metadata={"source": "api_network_ont_bulk"},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return OntBulkActionResponse(
            task_id=result.orchestrator_task_id or "",
            bulk_run_id=str(result.run_id),
            correlation_key=result.correlation_key,
            message=(
                f"Bulk provisioning queued for {result.processed} ONT(s); "
                f"{result.skipped} skipped"
            ),
        )
    from app.services import web_admin as web_admin_service
    from app.services.queue_adapter import enqueue_task

    bulk_params = dict(payload.params or {})
    if payload.action == "wifi_update":
        # The lifecycle records who asked; the permission was checked above.
        auth = getattr(request.state, "auth", {}) or {}
        bulk_params["_actor"] = (
            f"{auth.get('principal_type') or 'user'}:"
            f"{web_admin_service.get_actor_id(request) or auth.get('principal_id') or 'unknown'}"
        )
    task = enqueue_task(
        "app.tasks.ont_bulk.execute_bulk_action",
        args=[payload.ont_ids, payload.action, bulk_params],
        correlation_id=f"ont_bulk:{payload.action}:{len(payload.ont_ids)}",
        source="api_network_ont_bulk",
    )
    return OntBulkActionResponse(
        task_id=task.task_id or "",
        message=f"Bulk {payload.action} queued for {len(payload.ont_ids)} ONT(s)",
    )


@router.get(
    "/ont-units/bulk-action/{task_id}",
    response_model=OntBulkActionStatus,
    dependencies=[Depends(require_permission("network:ont:read"))],
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


@router.get(
    "/ont-units/bulk-provisioning/{run_id}",
    response_model=BulkProvisioningRunRead,
    dependencies=[Depends(require_permission("network:ont:read"))],
)
def get_bulk_provisioning_run(
    run_id: str,
    db: Session = Depends(get_db),
) -> BulkProvisioningRunRead:
    from app.services.network.bulk_provisioning import (
        get_bulk_provisioning_run as load_bulk_run,
    )
    from app.services.network.bulk_provisioning import (
        list_bulk_provisioning_events,
    )

    run = load_bulk_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Bulk provisioning run not found")
    payload = BulkProvisioningRunRead.model_validate(run)
    payload.event_count = len(list_bulk_provisioning_events(db, run_id))
    return payload
