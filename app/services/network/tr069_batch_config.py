"""TR-069 parameter batching for efficient ACS task submission.

Collects multiple TR-069 configuration parameters and submits them as a
single setParameterValues task to the ACS, reducing task queue overhead
and network round-trips.

Usage:
    from app.services.network.tr069_batch_config import Tr069ConfigBatch

    batch = Tr069ConfigBatch()
    batch.add_connection_request_credentials("user", "pass", interval=300)
    batch.add_pppoe_credentials(wan_path="...", username="user", password="pass")
    batch.add_lan_config(lan_ip="192.168.1.1", subnet="255.255.255.0")
    batch.add_wifi_config(ssid="MyNetwork", password="secret", enabled=True)

    success, message, data = batch.submit(client, device_id)

Expected Impact:
- Task queue: 5+ tasks per ONT -> 1 task per ONT
- ACS API load: ~40% reduction during bootstrap
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# TR-181 parameter paths (Device.*)
TR181_PATHS = {
    "cr_username": "Device.ManagementServer.ConnectionRequestUsername",
    "cr_password": "Device.ManagementServer.ConnectionRequestPassword",
    "periodic_inform_enable": "Device.ManagementServer.PeriodicInformEnable",
    "periodic_inform_interval": "Device.ManagementServer.PeriodicInformInterval",
    "lan_ip": "Device.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
    "lan_subnet": "Device.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
    "dhcp_enabled": "Device.LANDevice.1.LANHostConfigManagement.DHCPServerConfigurable",
    "dhcp_start": "Device.LANDevice.1.LANHostConfigManagement.MinAddress",
    "dhcp_end": "Device.LANDevice.1.LANHostConfigManagement.MaxAddress",
}

# TR-098 parameter paths (InternetGatewayDevice.*)
TR098_PATHS = {
    "cr_username": "InternetGatewayDevice.ManagementServer.ConnectionRequestUsername",
    "cr_password": "InternetGatewayDevice.ManagementServer.ConnectionRequestPassword",
    "periodic_inform_enable": "InternetGatewayDevice.ManagementServer.PeriodicInformEnable",
    "periodic_inform_interval": "InternetGatewayDevice.ManagementServer.PeriodicInformInterval",
    "lan_ip": "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
    "lan_subnet": "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
    "dhcp_enabled": "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerConfigurable",
    "dhcp_start": "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress",
    "dhcp_end": "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress",
}


@dataclass
class Tr069ConfigBatch:
    """Collects TR-069 configuration parameters for batch submission.

    Supports both TR-181 (Device.) and TR-098 (InternetGatewayDevice.) data models.
    The data model is auto-detected from the device or can be set explicitly.
    """

    parameters: dict[str, Any] = field(default_factory=dict)
    data_model: str = "Device"  # "Device" (TR-181) or "InternetGatewayDevice" (TR-098)

    def __post_init__(self) -> None:
        """Validate data model."""
        if self.data_model not in ("Device", "InternetGatewayDevice"):
            raise ValueError(f"Invalid data model: {self.data_model}")

    @property
    def paths(self) -> dict[str, str]:
        """Get path mapping for current data model."""
        if self.data_model == "InternetGatewayDevice":
            return TR098_PATHS
        return TR181_PATHS

    @property
    def is_empty(self) -> bool:
        """True if no parameters have been added."""
        return len(self.parameters) == 0

    @property
    def parameter_count(self) -> int:
        """Number of parameters in batch."""
        return len(self.parameters)

    def add_parameter(self, path: str, value: Any) -> None:
        """Add a raw parameter to the batch.

        Args:
            path: Full CWMP parameter path
            value: Parameter value
        """
        self.parameters[path] = value

    def add_connection_request_credentials(
        self,
        username: str,
        password: str,
        *,
        periodic_inform_interval: int = 300,
        periodic_inform_enable: bool = True,
    ) -> None:
        """Add connection request credentials to batch.

        Args:
            username: CR username
            password: CR password
            periodic_inform_interval: Inform interval in seconds (default 300)
            periodic_inform_enable: Enable periodic inform (default True)
        """
        paths = self.paths
        self.parameters[paths["cr_username"]] = username
        self.parameters[paths["cr_password"]] = password
        self.parameters[paths["periodic_inform_enable"]] = periodic_inform_enable
        self.parameters[paths["periodic_inform_interval"]] = periodic_inform_interval

    def add_pppoe_credentials(
        self,
        *,
        wan_path: str,
        username: str,
        password: str,
    ) -> None:
        """Add PPPoE credentials to batch.

        Args:
            wan_path: WAN connection path (e.g., "Device.PPP.Interface.1.")
            username: PPPoE username
            password: PPPoE password
        """
        # Normalize path
        base_path = wan_path.rstrip(".")
        self.parameters[f"{base_path}.Username"] = username
        self.parameters[f"{base_path}.Password"] = password

    def add_lan_config(
        self,
        *,
        lan_ip: str | None = None,
        subnet: str | None = None,
        dhcp_enabled: bool | None = None,
        dhcp_start: str | None = None,
        dhcp_end: str | None = None,
    ) -> None:
        """Add LAN configuration to batch.

        Args:
            lan_ip: LAN IP address
            subnet: Subnet mask
            dhcp_enabled: Enable DHCP server
            dhcp_start: DHCP range start
            dhcp_end: DHCP range end
        """
        paths = self.paths
        if lan_ip is not None:
            self.parameters[paths["lan_ip"]] = lan_ip
        if subnet is not None:
            self.parameters[paths["lan_subnet"]] = subnet
        if dhcp_enabled is not None:
            self.parameters[paths["dhcp_enabled"]] = dhcp_enabled
        if dhcp_start is not None:
            self.parameters[paths["dhcp_start"]] = dhcp_start
        if dhcp_end is not None:
            self.parameters[paths["dhcp_end"]] = dhcp_end

    def add_wifi_config(
        self,
        *,
        ssid: str | None = None,
        password: str | None = None,
        enabled: bool | None = None,
        channel: int | None = None,
        security_mode: str | None = None,
        wifi_path: str | None = None,
    ) -> None:
        """Add WiFi configuration to batch.

        Args:
            ssid: WiFi SSID
            password: WiFi password (WPA key)
            enabled: Enable/disable WiFi
            channel: WiFi channel (0 for auto)
            security_mode: Security mode (e.g., "WPA2-Personal")
            wifi_path: Custom WiFi path (auto-detected if None)
        """
        # Default WiFi paths
        if self.data_model == "Device":
            base_path = wifi_path or "Device.WiFi.SSID.1."
            radio_path = "Device.WiFi.Radio.1."
        else:
            base_path = wifi_path or "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1."
            radio_path = base_path

        base_path = base_path.rstrip(".")
        radio_path = radio_path.rstrip(".")

        if ssid is not None:
            self.parameters[f"{base_path}.SSID"] = ssid
        if password is not None:
            # Path varies by data model
            if self.data_model == "Device":
                self.parameters[f"{base_path}.X_HW_PreSharedKey"] = password
            else:
                self.parameters[f"{base_path}.PreSharedKey.1.PreSharedKey"] = password
        if enabled is not None:
            self.parameters[f"{base_path}.Enable"] = enabled
        if channel is not None:
            self.parameters[f"{radio_path}.Channel"] = channel
        if security_mode is not None:
            if self.data_model == "Device":
                self.parameters[f"{base_path}.X_HW_SecurityMode"] = security_mode
            else:
                self.parameters[f"{base_path}.BeaconType"] = security_mode

    def submit(
        self,
        client,
        device_id: str,
        *,
        trigger_inform: bool = True,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Submit all parameters as a single setParameterValues task.

        Args:
            client: GenieACS client instance
            device_id: GenieACS device ID
            trigger_inform: Send connection request after submission

        Returns:
            (success, message, data) tuple where data contains task info
        """
        if self.is_empty:
            return True, "No parameters to submit (batch empty)", {}

        logger.info(
            "Submitting batched TR-069 config: device_id=%s parameter_count=%d",
            device_id,
            self.parameter_count,
        )

        try:
            # Submit as single setParameterValues task
            task_result = client.set_parameter_values(device_id, self.parameters)

            task_id = task_result.get("_id")
            already_pending = task_result.get("alreadyPending", False)

            if already_pending:
                logger.warning(
                    "Batched TR-069 config merged with pending task: device_id=%s",
                    device_id,
                )
                return (
                    True,
                    "Configuration merged with pending task",
                    {
                        "task_id": task_id,
                        "parameter_count": self.parameter_count,
                        "merged": True,
                    },
                )

            logger.info(
                "Batched TR-069 config submitted: device_id=%s task_id=%s",
                device_id,
                task_id,
            )

            # Optionally trigger immediate Inform
            if trigger_inform:
                try:
                    client.send_connection_request(device_id)
                except Exception as cr_exc:
                    logger.warning(
                        "Connection request failed after batch submit: %s",
                        cr_exc,
                    )

            return (
                True,
                f"Configuration submitted ({self.parameter_count} parameters)",
                {
                    "task_id": task_id,
                    "parameter_count": self.parameter_count,
                    "parameters": list(self.parameters.keys()),
                },
            )

        except Exception as exc:
            logger.exception(
                "Failed to submit batched TR-069 config: device_id=%s",
                device_id,
            )
            return (
                False,
                f"Failed to submit configuration: {exc}",
                {"error": str(exc)},
            )

    @classmethod
    def from_ont_config(
        cls,
        ont,
        profile,
        *,
        cr_username: str | None = None,
        cr_password: str | None = None,
        periodic_inform_interval: int = 300,
    ) -> Tr069ConfigBatch:
        """Create batch from ONT and provisioning profile.

        Args:
            ont: OntUnit model instance
            profile: OntProvisioningProfile model instance
            cr_username: Connection request username (or from profile)
            cr_password: Connection request password (or from profile)
            periodic_inform_interval: Inform interval in seconds

        Returns:
            Tr069ConfigBatch populated with ONT configuration
        """
        # Detect data model from ONT
        data_model = getattr(ont, "tr069_data_model", None) or "Device"

        batch = cls(data_model=data_model)

        # Connection request credentials
        cr_user = cr_username or getattr(profile, "cr_username", None)
        cr_pass = cr_password or getattr(profile, "cr_password", None)
        if cr_user and cr_pass:
            batch.add_connection_request_credentials(
                cr_user,
                cr_pass,
                periodic_inform_interval=periodic_inform_interval,
            )

        # LAN configuration from ONT
        lan_ip = getattr(ont, "lan_gateway_ip", None)
        lan_subnet = getattr(ont, "lan_subnet_mask", None)
        dhcp_enabled = getattr(ont, "lan_dhcp_enabled", None)
        dhcp_start = getattr(ont, "lan_dhcp_start", None)
        dhcp_end = getattr(ont, "lan_dhcp_end", None)

        if any(v is not None for v in [lan_ip, lan_subnet, dhcp_enabled, dhcp_start, dhcp_end]):
            batch.add_lan_config(
                lan_ip=lan_ip,
                subnet=lan_subnet,
                dhcp_enabled=dhcp_enabled,
                dhcp_start=dhcp_start,
                dhcp_end=dhcp_end,
            )

        # WiFi configuration from ONT
        wifi_ssid = getattr(ont, "wifi_ssid", None)
        wifi_password = getattr(ont, "wifi_password", None)
        wifi_enabled = getattr(ont, "wifi_enabled", None)
        wifi_channel = getattr(ont, "wifi_channel", None)

        if any(v is not None for v in [wifi_ssid, wifi_password, wifi_enabled, wifi_channel]):
            # Decrypt password if encrypted
            decrypted_password = None
            if wifi_password:
                try:
                    from app.services.credential_crypto import decrypt_credential
                    decrypted_password = decrypt_credential(wifi_password)
                except Exception:
                    decrypted_password = str(wifi_password)

            batch.add_wifi_config(
                ssid=wifi_ssid,
                password=decrypted_password,
                enabled=wifi_enabled,
                channel=wifi_channel,
            )

        return batch


def submit_batched_config(
    db,
    ont_id: str,
    batch: Tr069ConfigBatch,
) -> tuple[bool, str, dict[str, Any]]:
    """Submit batched configuration for an ONT.

    Resolves the GenieACS client and device ID, then submits the batch.

    Args:
        db: Database session
        ont_id: ONT unit ID
        batch: Configured Tr069ConfigBatch

    Returns:
        (success, message, data) tuple
    """
    from app.services.network.ont_action_common import get_ont_client_or_error

    if batch.is_empty:
        return True, "No parameters to submit (batch empty)", {}

    resolved, error = get_ont_client_or_error(db, ont_id)
    if error:
        return False, error.message, {}
    if resolved is None:
        return False, "No ACS device resolved for this ONT", {}

    _ont, client, device_id = resolved

    return batch.submit(client, device_id)
