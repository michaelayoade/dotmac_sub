"""Device lifecycle and config retrieval actions for CPE devices."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    DeviceConfig,
    get_cpe_or_error,
    resolve_cpe_client_or_error,
)

logger = logging.getLogger(__name__)

_DEVICE_INFO_PARAMS = [
    "Device.DeviceInfo.Manufacturer",
    "Device.DeviceInfo.ModelName",
    "Device.DeviceInfo.SerialNumber",
    "Device.DeviceInfo.SoftwareVersion",
    "Device.DeviceInfo.HardwareVersion",
    "Device.DeviceInfo.UpTime",
    "Device.DeviceInfo.MemoryStatus.Total",
    "Device.DeviceInfo.MemoryStatus.Free",
]

_WAN_PARAMS = [
    "Device.IP.Interface.1.IPv4Address.1.IPAddress",
    "Device.IP.Interface.1.IPv4Address.1.SubnetMask",
    "Device.IP.Interface.1.Status",
    "Device.DHCPv4.Client.1.IPAddress",
]

_WIFI_PARAMS = [
    "Device.WiFi.SSID.1.SSID",
    "Device.WiFi.SSID.1.Enable",
    "Device.WiFi.Radio.1.Channel",
    "Device.WiFi.Radio.1.OperatingStandards",
]


def reboot(db: Session, cpe_id: str) -> ActionResult:
    """Reboot a CPE device via TR-069."""
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.reboot_device(device_id)
        logger.info("Reboot sent to CPE %s (device %s)", cpe.serial_number, device_id)
        return ActionResult(
            success=True,
            message=f"Reboot command sent to {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Reboot failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(success=False, message=f"Reboot failed: {exc}")


def refresh_status(db: Session, cpe_id: str) -> ActionResult:
    """Refresh CPE device parameters from GenieACS."""
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.refresh_object(device_id, "Device.", connection_request=True)
        logger.info("Refresh sent to CPE %s (device %s)", cpe.serial_number, device_id)
        return ActionResult(
            success=True,
            message=f"Status refresh requested for {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Refresh failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(success=False, message=f"Status refresh failed: {exc}")


def get_running_config(db: Session, cpe_id: str) -> ActionResult:
    """Fetch running configuration from a CPE device."""
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        logger.error("Config fetch failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to fetch config: {exc}")

    def _extract(params: list[str]) -> dict[str, object]:
        return {
            p.rsplit(".", 1)[-1]: client.extract_parameter_value(device, p)
            for p in params
        }

    config = DeviceConfig(
        device_info=_extract(_DEVICE_INFO_PARAMS),
        wan=_extract(_WAN_PARAMS),
        optical={},
        wifi=_extract(_WIFI_PARAMS),
        raw=device,
    )
    return ActionResult(
        success=True,
        message="Configuration retrieved.",
        data={
            "device_info": config.device_info,
            "wan": config.wan,
            "wifi": config.wifi,
        },
    )


def factory_reset(db: Session, cpe_id: str) -> ActionResult:
    """Factory reset a CPE device via TR-069."""
    cpe, error = get_cpe_or_error(db, cpe_id)
    if error:
        return error
    assert cpe is not None  # noqa: S101
    resolved, error = resolve_cpe_client_or_error(db, cpe)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.factory_reset(device_id)
        logger.info("Factory reset sent to CPE %s (device %s)", cpe.serial_number, device_id)
        return ActionResult(
            success=True,
            message=f"Factory reset command sent to {cpe.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Factory reset failed for CPE %s: %s", cpe.serial_number, exc)
        return ActionResult(success=False, message=f"Factory reset failed: {exc}")
