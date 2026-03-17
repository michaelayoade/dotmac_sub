"""ONT remote action service via GenieACS TR-069.

Provides reboot, refresh, factory reset, and running config retrieval
for ONT devices by mapping OntUnit serial numbers to GenieACS device IDs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OntUnit
from app.services.genieacs import GenieACSError
from app.services.network._resolve import resolve_genieacs_with_reason

logger = logging.getLogger(__name__)

# TR-069 parameter paths commonly found on GPON ONTs
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


@dataclass
class ActionResult:
    """Result of a remote ONT action."""

    success: bool
    message: str
    data: dict[str, Any] | None = None


@dataclass
class DeviceConfig:
    """Structured running config from an ONT."""

    device_info: dict[str, Any]
    wan: dict[str, Any]
    optical: dict[str, Any]
    wifi: dict[str, Any]
    raw: dict[str, Any]


class OntActions:
    """Remote ONT action dispatcher using GenieACS."""

    @staticmethod
    def _resolve_or_error(db: Session, ont: OntUnit) -> tuple[Any | None, str | None]:
        resolved, reason = resolve_genieacs_with_reason(db, ont)
        if not resolved:
            return None, reason
        return resolved, None

    @staticmethod
    def reboot(db: Session, ont_id: str) -> ActionResult:
        """Send reboot command to ONT via GenieACS.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            ActionResult with success/failure info.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        try:
            result = client.reboot_device(device_id)
            logger.info("Reboot sent to ONT %s (device %s)", ont.serial_number, device_id)
            return ActionResult(
                success=True,
                message=f"Reboot command sent to {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error("Reboot failed for ONT %s: %s", ont.serial_number, e)
            return ActionResult(
                success=False,
                message=f"Reboot failed: {e}",
            )

    @staticmethod
    def refresh_status(db: Session, ont_id: str) -> ActionResult:
        """Force a connection request to pull latest parameters.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            ActionResult with success/failure info.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        try:
            result = client.refresh_object(device_id, "Device.", connection_request=True)
            logger.info(
                "Refresh sent to ONT %s (device %s)", ont.serial_number, device_id
            )
            return ActionResult(
                success=True,
                message=f"Status refresh requested for {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error("Refresh failed for ONT %s: %s", ont.serial_number, e)
            return ActionResult(
                success=False,
                message=f"Status refresh failed: {e}",
            )

    @staticmethod
    def get_running_config(db: Session, ont_id: str) -> ActionResult:
        """Fetch current device parameters grouped into sections.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            ActionResult with DeviceConfig in data.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        try:
            device = client.get_device(device_id)
        except GenieACSError as e:
            logger.error("Config fetch failed for ONT %s: %s", ont.serial_number, e)
            return ActionResult(success=False, message=f"Failed to fetch config: {e}")

        def _extract(params: list[str]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for p in params:
                val = client.extract_parameter_value(device, p)
                # Use last segment as key for display
                key = p.rsplit(".", 1)[-1]
                result[key] = val
            return result

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

    @staticmethod
    def set_wifi_ssid(db: Session, ont_id: str, ssid: str) -> ActionResult:
        """Set WiFi SSID via GenieACS setParameterValues task.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            ssid: New SSID value.

        Returns:
            ActionResult with success/failure info.
        """
        if not ssid or len(ssid) > 32:
            return ActionResult(
                success=False,
                message="SSID must be 1-32 characters.",
            )

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        params = {
            "Device.WiFi.SSID.1.SSID": ssid,
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID": ssid,
        }
        try:
            result = client.set_parameter_values(device_id, params)
            logger.info(
                "WiFi SSID set on ONT %s to '%s'",
                ont.serial_number,
                ssid,
            )
            return ActionResult(
                success=True,
                message=f"WiFi SSID updated to '{ssid}' on {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error("Set WiFi SSID failed for ONT %s: %s", ont.serial_number, e)
            return ActionResult(success=False, message=f"Failed to set SSID: {e}")

    @staticmethod
    def set_wifi_password(db: Session, ont_id: str, password: str) -> ActionResult:
        """Set WiFi password via GenieACS setParameterValues task.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            password: New WiFi password.

        Returns:
            ActionResult with success/failure info.
        """
        if not password or len(password) < 8:
            return ActionResult(
                success=False,
                message="WiFi password must be at least 8 characters.",
            )

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        params = {
            "Device.WiFi.AccessPoint.1.Security.KeyPassphrase": password,
            "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.KeyPassphrase": password,
        }
        try:
            result = client.set_parameter_values(device_id, params)
            logger.info(
                "WiFi password set on ONT %s",
                ont.serial_number,
            )
            return ActionResult(
                success=True,
                message=f"WiFi password updated on {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error(
                "Set WiFi password failed for ONT %s: %s", ont.serial_number, e
            )
            return ActionResult(
                success=False, message=f"Failed to set WiFi password: {e}"
            )

    @staticmethod
    def toggle_lan_port(
        db: Session, ont_id: str, port: int, enabled: bool
    ) -> ActionResult:
        """Enable or disable a LAN Ethernet port via TR-069.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            port: Port number (1-4).
            enabled: True to enable, False to disable.

        Returns:
            ActionResult with success/failure info.
        """
        if port < 1 or port > 4:
            return ActionResult(
                success=False, message="Port number must be between 1 and 4."
            )

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        value = "true" if enabled else "false"
        params = {
            f"Device.Ethernet.Interface.{port}.Enable": value,
            f"InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.{port}.Enable": value,
        }
        try:
            result = client.set_parameter_values(device_id, params)
            action_word = "enabled" if enabled else "disabled"
            logger.info(
                "LAN port %d %s on ONT %s", port, action_word, ont.serial_number
            )
            return ActionResult(
                success=True,
                message=f"LAN port {port} {action_word} on {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error(
                "Toggle LAN port %d failed for ONT %s: %s",
                port,
                ont.serial_number,
                e,
            )
            return ActionResult(
                success=False, message=f"Failed to toggle LAN port: {e}"
            )

    @staticmethod
    def factory_reset(db: Session, ont_id: str) -> ActionResult:
        """Send factory reset command to ONT via GenieACS.

        This is a destructive action and should require confirmation.

        Args:
            db: Database session.
            ont_id: OntUnit ID.

        Returns:
            ActionResult with success/failure info.
        """
        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        try:
            result = client.factory_reset(device_id)
            logger.info(
                "Factory reset sent to ONT %s (device %s)",
                ont.serial_number,
                device_id,
            )
            return ActionResult(
                success=True,
                message=f"Factory reset command sent to {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error("Factory reset failed for ONT %s: %s", ont.serial_number, e)
            return ActionResult(
                success=False,
                message=f"Factory reset failed: {e}",
            )

    @staticmethod
    def firmware_upgrade(
        db: Session,
        ont_id: str,
        firmware_image_id: str,
    ) -> ActionResult:
        """Trigger firmware upgrade on ONT via TR-069 Download RPC.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            firmware_image_id: OntFirmwareImage ID.

        Returns:
            ActionResult with success/failure info.
        """
        from app.models.network import OntFirmwareImage

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        firmware = db.get(OntFirmwareImage, firmware_image_id)
        if not firmware:
            return ActionResult(success=False, message="Firmware image not found.")
        if not firmware.is_active:
            return ActionResult(success=False, message="Firmware image is not active.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

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
        except GenieACSError as e:
            logger.error(
                "Firmware upgrade failed for ONT %s: %s", ont.serial_number, e
            )
            return ActionResult(
                success=False,
                message=f"Firmware upgrade failed: {e}",
            )


    @staticmethod
    def set_pppoe_credentials(
        db: Session, ont_id: str, username: str, password: str
    ) -> ActionResult:
        """Push PPPoE username and password to ONT via TR-069 SetParameterValues.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            username: PPPoE username.
            password: PPPoE password.

        Returns:
            ActionResult with success/failure info.
        """
        if not username:
            return ActionResult(success=False, message="PPPoE username is required.")
        if not password:
            return ActionResult(success=False, message="PPPoE password is required.")

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        # Dual-path: TR-181 (Device) and TR-098 (InternetGatewayDevice)
        params = {
            "Device.PPP.Interface.1.Username": username,
            "Device.PPP.Interface.1.Password": password,
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username": username,
            "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password": password,
        }
        try:
            result = client.set_parameter_values(device_id, params)
            # Update observed PPPoE username on the ONT record
            ont.pppoe_username = username
            db.flush()
            logger.info(
                "PPPoE credentials set on ONT %s (user: %s)",
                ont.serial_number, username,
            )
            return ActionResult(
                success=True,
                message=f"PPPoE credentials pushed to {ont.serial_number}.",
                data=result,
            )
        except GenieACSError as e:
            logger.error(
                "Set PPPoE credentials failed for ONT %s: %s", ont.serial_number, e
            )
            return ActionResult(
                success=False, message=f"Failed to set PPPoE credentials: {e}"
            )

    @staticmethod
    def run_ping_diagnostic(
        db: Session, ont_id: str, host: str, count: int = 4
    ) -> ActionResult:
        """Run IP ping diagnostic from the ONT via TR-069.

        Uses the TR-069 IPPingDiagnostics object to trigger a ping test
        from the ONT itself to a target host.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            host: Target hostname or IP to ping.
            count: Number of ping repetitions (default 4).

        Returns:
            ActionResult with diagnostic results.
        """
        if not host or not host.strip():
            return ActionResult(success=False, message="Ping target host is required.")

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        count = max(1, min(count, 20))  # Clamp 1-20
        params = {
            "Device.IP.Diagnostics.IPPing.Host": host.strip(),
            "Device.IP.Diagnostics.IPPing.NumberOfRepetitions": str(count),
            "Device.IP.Diagnostics.IPPing.DiagnosticsState": "Requested",
            "InternetGatewayDevice.IPPingDiagnostics.Host": host.strip(),
            "InternetGatewayDevice.IPPingDiagnostics.NumberOfRepetitions": str(count),
            "InternetGatewayDevice.IPPingDiagnostics.DiagnosticsState": "Requested",
        }
        try:
            result = client.set_parameter_values(device_id, params)
            logger.info(
                "Ping diagnostic started on ONT %s → %s (%d pings)",
                ont.serial_number, host.strip(), count,
            )
            return ActionResult(
                success=True,
                message=f"Ping diagnostic started on {ont.serial_number} → {host.strip()} ({count} pings). Results will appear after the next device inform.",
                data=result,
            )
        except GenieACSError as e:
            logger.error(
                "Ping diagnostic failed for ONT %s: %s", ont.serial_number, e
            )
            return ActionResult(
                success=False, message=f"Failed to start ping diagnostic: {e}"
            )

    @staticmethod
    def run_traceroute_diagnostic(
        db: Session, ont_id: str, host: str
    ) -> ActionResult:
        """Run traceroute diagnostic from the ONT via TR-069.

        Args:
            db: Database session.
            ont_id: OntUnit ID.
            host: Target hostname or IP to trace.

        Returns:
            ActionResult with diagnostic results.
        """
        if not host or not host.strip():
            return ActionResult(success=False, message="Traceroute target host is required.")

        ont = db.get(OntUnit, ont_id)
        if not ont:
            return ActionResult(success=False, message="ONT not found.")

        resolved, reason = OntActions._resolve_or_error(db, ont)
        if not resolved:
            return ActionResult(
                success=False,
                message=reason or "No GenieACS server configured for this ONT.",
            )

        client, device_id = resolved
        params = {
            "Device.IP.Diagnostics.TraceRoute.Host": host.strip(),
            "Device.IP.Diagnostics.TraceRoute.DiagnosticsState": "Requested",
            "InternetGatewayDevice.TraceRouteDiagnostics.Host": host.strip(),
            "InternetGatewayDevice.TraceRouteDiagnostics.DiagnosticsState": "Requested",
        }
        try:
            result = client.set_parameter_values(device_id, params)
            logger.info(
                "Traceroute diagnostic started on ONT %s → %s",
                ont.serial_number, host.strip(),
            )
            return ActionResult(
                success=True,
                message=f"Traceroute started on {ont.serial_number} → {host.strip()}. Results will appear after the next device inform.",
                data=result,
            )
        except GenieACSError as e:
            logger.error(
                "Traceroute diagnostic failed for ONT %s: %s", ont.serial_number, e
            )
            return ActionResult(
                success=False, message=f"Failed to start traceroute: {e}"
            )


ont_actions = OntActions()
