/**
 * Virtual Parameter: WAN_Status
 * Returns the WAN connection status (Connected/Disconnected/etc.)
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) paths first
const devicePPP = declare("Device.PPP.Interface.*.ConnectionStatus", { value: 1 });
if (devicePPP.value !== undefined) {
  return { writable: false, value: devicePPP.value };
}

const deviceIP = declare("Device.IP.Interface.1.Status", { value: 1 });
if (deviceIP.value !== undefined) {
  return { writable: false, value: deviceIP.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdPPP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ConnectionStatus", { value: 1 });
if (igdPPP.value !== undefined) {
  return { writable: false, value: igdPPP.value };
}

const igdIP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.ConnectionStatus", { value: 1 });
if (igdIP.value !== undefined) {
  return { writable: false, value: igdIP.value };
}

return { writable: false, value: null };
