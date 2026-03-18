"""Device lifecycle and config retrieval actions for ONTs."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    DeviceConfig,
    get_ont_or_error,
    resolve_client_or_error,
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

_OPTICAL_PARAMS = [
    "Device.Optical.Interface.1.OpticalSignalLevel",
    "Device.Optical.Interface.1.LowerOpticalThreshold",
    "Device.Optical.Interface.1.UpperOpticalThreshold",
    "Device.Optical.Interface.1.TransmitOpticalLevel",
]

_WIFI_PARAMS = [
    "Device.WiFi.SSID.1.SSID",
    "Device.WiFi.SSID.1.Enable",
    "Device.WiFi.Radio.1.Channel",
    "Device.WiFi.Radio.1.OperatingStandards",
]


def reboot(db: Session, ont_id: str) -> ActionResult:
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.reboot_device(device_id)
        logger.info("Reboot sent to ONT %s (device %s)", ont.serial_number, device_id)
        return ActionResult(
            success=True,
            message=f"Reboot command sent to {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Reboot failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Reboot failed: {exc}")


def refresh_status(db: Session, ont_id: str) -> ActionResult:
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.refresh_object(device_id, "Device.", connection_request=True)
        logger.info("Refresh sent to ONT %s (device %s)", ont.serial_number, device_id)
        return ActionResult(
            success=True,
            message=f"Status refresh requested for {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Refresh failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Status refresh failed: {exc}")


def get_running_config(db: Session, ont_id: str) -> ActionResult:
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        logger.error("Config fetch failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to fetch config: {exc}")

    def _extract(params: list[str]) -> dict[str, object]:
        return {
            p.rsplit(".", 1)[-1]: client.extract_parameter_value(device, p)
            for p in params
        }

    config = DeviceConfig(
        device_info=_extract(_DEVICE_INFO_PARAMS),
        wan=_extract(_WAN_PARAMS),
        optical=_extract(_OPTICAL_PARAMS),
        wifi=_extract(_WIFI_PARAMS),
        raw=device,
    )
    return ActionResult(
        success=True,
        message="Configuration retrieved.",
        data={
            "device_info": config.device_info,
            "wan": config.wan,
            "optical": config.optical,
            "wifi": config.wifi,
        },
    )


def factory_reset(db: Session, ont_id: str) -> ActionResult:
    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101
    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.factory_reset(device_id)
        logger.info("Factory reset sent to ONT %s (device %s)", ont.serial_number, device_id)
        return ActionResult(
            success=True,
            message=f"Factory reset command sent to {ont.serial_number}.",
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Factory reset failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Factory reset failed: {exc}")


def firmware_upgrade(db: Session, ont_id: str, firmware_image_id: str) -> ActionResult:
    from app.models.network import OntFirmwareImage

    ont, error = get_ont_or_error(db, ont_id)
    if error:
        return error
    assert ont is not None  # noqa: S101

    firmware = db.get(OntFirmwareImage, firmware_image_id)
    if not firmware:
        return ActionResult(success=False, message="Firmware image not found.")
    if not firmware.is_active:
        return ActionResult(success=False, message="Firmware image is not active.")

    resolved, error = resolve_client_or_error(db, ont)
    if error:
        return error
    assert resolved is not None  # noqa: S101

    client, device_id = resolved
    try:
        result = client.download(
            device_id,
            file_type="1 Firmware Upgrade Image",
            file_url=firmware.file_url,
            filename=firmware.filename,
        )
        logger.info(
            "Firmware upgrade triggered for ONT %s → %s v%s",
            ont.serial_number,
            firmware.vendor,
            firmware.version,
        )
        return ActionResult(
            success=True,
            message=(
                f"Firmware upgrade to v{firmware.version} initiated for "
                f"{ont.serial_number}. The ONT will download and reboot."
            ),
            data=result,
        )
    except GenieACSError as exc:
        logger.error("Firmware upgrade failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Firmware upgrade failed: {exc}")
