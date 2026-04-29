/**
 * Virtual Parameter: WAN_Connection_Type
 * Returns the WAN connection type (PPPoE, DHCP, Static, etc.)
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-098 PPP connection type
const igdPPP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ConnectionType", { value: 1 });
if (igdPPP.value !== undefined) {
  return { writable: false, value: igdPPP.value };
}

// Try TR-098 IP connection type
const igdIP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.ConnectionType", { value: 1 });
if (igdIP.value !== undefined) {
  return { writable: false, value: igdIP.value };
}

// Try TR-181 PPP status (indicates PPPoE if present)
const devicePPP = declare("Device.PPP.Interface.*.ConnectionStatus", { value: 1 });
if (devicePPP.value !== undefined) {
  return { writable: false, value: "PPPoE" };
}

return { writable: false, value: null };
