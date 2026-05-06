"""GenieACS webhook receivers.

Receives callbacks from GenieACS:
- Inform webhook: CPE device inform messages
- Auth webhook: Credential lookups for CPE/CR authentication
- Device config: WiFi/service config for provision scripts
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.credential_crypto import decrypt_credential
from app.services.genieacs_service import genieacs_service
from app.services.network.effective_ont_config import resolve_effective_ont_config
from app.services.network.ont_serials import find_unique_active_ont_by_serial

router = APIRouter(prefix="/tr069", tags=["tr069-webhooks"])


class InformPayload(BaseModel):
    """GenieACS inform callback payload."""

    model_config = ConfigDict(extra="allow")

    serial_number: str | None = None
    oui: str | None = None
    product_class: str | None = None
    event: Any = Field(default="periodic")
    device_id: str | None = None
    request_id: str | None = None
    acs_server_id: str | None = None


@router.post("/inform")
def receive_inform(
    request: Request,
    payload: InformPayload,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Receive GenieACS inform webhook callback.

    GenieACS can be configured to POST to this endpoint on device inform.
    The payload contains device identity and event information.
    """
    acs = genieacs_service
    return acs.receive_inform(
        db,
        serial_number=payload.serial_number,
        device_id_raw=payload.device_id,
        event=payload.event,
        raw_payload=payload.model_dump(mode="json"),
        request_id=payload.request_id
        or request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id"),
        remote_addr=request.client.host if request.client else None,
        headers={
            "user-agent": request.headers.get("user-agent"),
            "x-forwarded-for": request.headers.get("x-forwarded-for"),
            "x-real-ip": request.headers.get("x-real-ip"),
        },
        oui=payload.oui,
        product_class=payload.product_class,
        acs_server_id=payload.acs_server_id,
    )


@router.get("/device-config/{serial_number}")
def get_device_config(
    serial_number: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get device configuration for GenieACS provision scripts.

    Called by GenieACS ext() function during bootstrap to fetch service
    config that needs to be re-applied after ONT reboot.

    TR-069 config is volatile (lost on reboot). OMCI handles persistent
    config (management IP, VLANs). This returns TR-069-only settings:
    - WiFi (SSID, password, channel, security)
    - WAN (PPPoE credentials, DHCP/static mode)
    - LAN (IP, subnet, DHCP server)
    - Access (remote management, HTTP, NAT)

    Returns:
        Full service config, or 404 if device not found.
    """
    ont = find_unique_active_ont_by_serial(db, serial_number)
    if not ont:
        raise HTTPException(status_code=404, detail="Device not found")

    # Resolve effective config (merges OLT defaults + per-ONT overrides)
    effective = resolve_effective_ont_config(db, ont)
    values = effective.get("values", {}) if isinstance(effective, dict) else {}

    def decrypt_if_needed(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return decrypt_credential(value)
        except Exception:
            return value  # Already decrypted or plain text

    # Build WiFi config
    wifi_config = None
    wifi_ssid = values.get("wifi_ssid")
    wifi_password = values.get("wifi_password")
    if wifi_ssid or wifi_password:
        wifi_config = {
            "ssid": wifi_ssid,
            "password": decrypt_if_needed(wifi_password),
            "enabled": values.get("wifi_enabled", True),
            "channel": values.get("wifi_channel", 0),  # 0 = auto
            "security_mode": values.get("wifi_security_mode", "WPA2"),
        }

    # Build WAN config (PPPoE/DHCP/Static)
    wan_config = None
    wan_mode = values.get("wan_mode")  # pppoe, dhcp, static
    pppoe_username = values.get("pppoe_username")
    pppoe_password = values.get("pppoe_password")
    if wan_mode or pppoe_username:
        wan_config = {
            "mode": wan_mode,
            "pppoe_username": pppoe_username,
            "pppoe_password": decrypt_if_needed(pppoe_password),
            "static_ip": values.get("wan_static_ip"),
            "static_gateway": values.get("wan_static_gateway"),
            "static_subnet": values.get("wan_static_subnet"),
            "static_dns": values.get("wan_static_dns"),
        }

    # Build LAN config
    lan_config = None
    lan_ip = values.get("lan_ip")
    lan_dhcp_enabled = values.get("lan_dhcp_enabled")
    if lan_ip or lan_dhcp_enabled is not None:
        lan_config = {
            "ip": lan_ip,
            "subnet": values.get("lan_subnet"),
            "dhcp_enabled": lan_dhcp_enabled,
            "dhcp_start": values.get("lan_dhcp_start"),
            "dhcp_end": values.get("lan_dhcp_end"),
        }

    # Build access/security config
    access_config = None
    wan_remote = values.get("wan_remote_access")
    mgmt_remote = values.get("mgmt_remote_access")
    http_mgmt = values.get("http_management")
    if wan_remote is not None or mgmt_remote is not None or http_mgmt is not None:
        access_config = {
            "wan_remote": wan_remote,
            "mgmt_remote": mgmt_remote,
            "http_management": http_mgmt,
        }

    return {
        "serial_number": serial_number,
        "ont_id": str(ont.id),
        "wifi": wifi_config,
        "wan": wan_config,
        "lan": lan_config,
        "access": access_config,
    }
