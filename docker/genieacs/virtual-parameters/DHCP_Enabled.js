/**
 * Virtual Parameter: DHCP_Enabled
 * Returns whether the DHCP server is enabled.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path
const deviceDHCP = declare("Device.DHCPv4.Server.Enable", { value: 1 });
if (deviceDHCP.value !== undefined) {
  return { writable: true, value: deviceDHCP.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdDHCP = declare("InternetGatewayDevice.LANDevice.*.LANHostConfigManagement.DHCPServerEnable", { value: 1 });
if (igdDHCP.value !== undefined) {
  return { writable: true, value: igdDHCP.value };
}

const igdDHCPFixed = declare("InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable", { value: 1 });
if (igdDHCPFixed.value !== undefined) {
  return { writable: true, value: igdDHCPFixed.value };
}

return { writable: false, value: null };
