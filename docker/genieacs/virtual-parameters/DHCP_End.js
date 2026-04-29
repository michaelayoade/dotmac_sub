/**
 * Virtual Parameter: DHCP_End
 * Returns the DHCP pool end address.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceEnd = declare("Device.DHCPv4.Server.Pool.1.MaxAddress", { value: 1 });
if (deviceEnd.value !== undefined) {
  return { writable: true, value: deviceEnd.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdEnd = declare("InternetGatewayDevice.LANDevice.*.LANHostConfigManagement.MaxAddress", { value: 1 });
if (igdEnd.value !== undefined) {
  return { writable: true, value: igdEnd.value };
}

const igdEndFixed = declare("InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress", { value: 1 });
if (igdEndFixed.value !== undefined) {
  return { writable: true, value: igdEndFixed.value };
}

return { writable: false, value: null };
