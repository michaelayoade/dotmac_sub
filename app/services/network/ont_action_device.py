"""Device lifecycle and config retrieval actions for ONTs."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.services.genieacs import GenieACSError
from app.services.network.ont_action_common import (
    ActionResult,
    DeviceConfig,
    detect_data_model_root,
    get_ont_client_or_error,
    get_ont_strict_or_error,
    persist_data_model_root,
)
from app.services.network.ont_tr069 import PARAM_GROUPS, _extract_first

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

_OPTICAL_CONFIG_PARAMS: dict[str, list[str]] = {
    "Optical Signal Level": [
        "Device.Optical.Interface.1.OpticalSignalLevel",
        "InternetGatewayDevice.WANDevice.*.X_HW_WANPONInterfaceConfig.RXPower",
        "InternetGatewayDevice.WANDevice.1.X_HW_WANPONInterfaceConfig.RXPower",
    ],
    "Lower Optical Threshold": ["Device.Optical.Interface.1.LowerOpticalThreshold"],
    "Upper Optical Threshold": ["Device.Optical.Interface.1.UpperOpticalThreshold"],
    "Transmit Optical Level": [
        "Device.Optical.Interface.1.TransmitOpticalLevel",
        "InternetGatewayDevice.WANDevice.*.X_HW_WANPONInterfaceConfig.TXPower",
        "InternetGatewayDevice.WANDevice.1.X_HW_WANPONInterfaceConfig.TXPower",
    ],
}

_RUNTIME_REFRESH_PARAMS = {
    "Device": [
        "Device.DeviceInfo.SerialNumber",
        "Device.DeviceInfo.SoftwareVersion",
        "Device.DeviceInfo.HardwareVersion",
        "Device.DeviceInfo.UpTime",
        "Device.ManagementServer.ConnectionRequestURL",
        "Device.ManagementServer.PeriodicInformEnable",
        "Device.ManagementServer.PeriodicInformInterval",
        "Device.PPP.Interface.1.Status",
        "Device.PPP.Interface.1.ConnectionStatus",
        "Device.PPP.Interface.1.Username",
        "Device.IP.Interface.1.Status",
        "Device.IP.Interface.1.IPv4Address.1.IPAddress",
        "Device.Hosts.HostNumberOfEntries",
        "Device.WiFi.SSID.1.SSID",
        "Device.WiFi.AccessPoint.1.AssociatedDeviceNumberOfEntries",
        "Device.Ethernet.Interface.1.Status",
        "Device.Ethernet.Interface.2.Status",
        "Device.Ethernet.Interface.3.Status",
        "Device.Ethernet.Interface.4.Status",
    ],
    "InternetGatewayDevice": [
        "InternetGatewayDevice.DeviceInfo.SerialNumber",
        "InternetGatewayDevice.DeviceInfo.SoftwareVersion",
        "InternetGatewayDevice.DeviceInfo.HardwareVersion",
        "InternetGatewayDevice.DeviceInfo.UpTime",
        "InternetGatewayDevice.ManagementServer.ConnectionRequestURL",
        "InternetGatewayDevice.ManagementServer.PeriodicInformEnable",
        "InternetGatewayDevice.ManagementServer.PeriodicInformInterval",
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ConnectionStatus",
        "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANIPConnection.1.ExternalIPAddress",
        "InternetGatewayDevice.LANDevice.1.Hosts.HostNumberOfEntries",
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
        "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.TotalAssociations",
        "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.1.Status",
        "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.2.Status",
        "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.3.Status",
        "InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.4.Status",
    ],
}


def reboot(db: Session, ont_id: str) -> ActionResult:
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
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
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    try:
        root = detect_data_model_root(db, ont, client, device_id)
        persist_data_model_root(ont, root)
        result = client.get_parameter_values(
            device_id,
            _RUNTIME_REFRESH_PARAMS.get(root, _RUNTIME_REFRESH_PARAMS["Device"]),
            connection_request=True,
        )
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
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    try:
        device = client.get_device(device_id)
    except GenieACSError as exc:
        logger.error("Config fetch failed for ONT %s: %s", ont.serial_number, exc)
        return ActionResult(success=False, message=f"Failed to fetch config: {exc}")

    def _extract_group(group_name: str) -> dict[str, object]:
        return {
            label: _extract_first(client, device, paths)
            for label, paths in PARAM_GROUPS.get(group_name, {}).items()
        }

    def _extract_custom_group(params: dict[str, list[str]]) -> dict[str, object]:
        return {
            label: _extract_first(client, device, paths)
            for label, paths in params.items()
        }

    config = DeviceConfig(
        device_info=_extract_group("system"),
        wan=_extract_group("wan"),
        optical=_extract_custom_group(_OPTICAL_CONFIG_PARAMS),
        wifi=_extract_group("wireless"),
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
    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")
    ont, client, device_id = resolved
    try:
        result = client.factory_reset(device_id)
        logger.info(
            "Factory reset sent to ONT %s (device %s)", ont.serial_number, device_id
        )
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

    ont, error = get_ont_strict_or_error(db, ont_id)
    if error:
        return error
    if ont is None:
        return ActionResult(success=False, message="ONT not found.")

    firmware = db.get(OntFirmwareImage, firmware_image_id)
    if not firmware:
        return ActionResult(success=False, message="Firmware image not found.")
    if not firmware.is_active:
        return ActionResult(success=False, message="Firmware image is not active.")

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return error
    if resolved is None:
        return ActionResult(success=False, message="ONT resolution failed.")

    _, client, device_id = resolved
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
