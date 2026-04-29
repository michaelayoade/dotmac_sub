/**
 * Virtual Parameter: DHCP_Start
 * Returns the DHCP pool start address.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceStart = declare("Device.DHCPv4.Server.Pool.1.MinAddress", { value: 1 });
if (deviceStart.value !== undefined) {
  return { writable: true, value: deviceStart.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdStart = declare("InternetGatewayDevice.LANDevice.*.LANHostConfigManagement.MinAddress", { value: 1 });
if (igdStart.value !== undefined) {
  return { writable: true, value: igdStart.value };
}

const igdStartFixed = declare("InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress", { value: 1 });
if (igdStartFixed.value !== undefined) {
  return { writable: true, value: igdStartFixed.value };
}

return { writable: false, value: null };
