/**
 * Virtual Parameter: WAN_Uptime
 * Returns the WAN connection uptime in seconds.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-098 PPP uptime
const igdPPP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Uptime", { value: 1 });
if (igdPPP.value !== undefined) {
  return { writable: false, value: igdPPP.value };
}

// Try TR-098 IP connection uptime
const igdIP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.Uptime", { value: 1 });
if (igdIP.value !== undefined) {
  return { writable: false, value: igdIP.value };
}

// TR-181 doesn't have a direct WAN uptime, use device uptime as fallback
const deviceUptime = declare("Device.DeviceInfo.UpTime", { value: 1 });
if (deviceUptime.value !== undefined) {
  return { writable: false, value: deviceUptime.value };
}

return { writable: false, value: null };
