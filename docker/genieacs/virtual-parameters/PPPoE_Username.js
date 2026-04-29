/**
 * Virtual Parameter: PPPoE_Username
 * Returns the PPPoE username configured on the device.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path
const deviceUsername = declare("Device.PPP.Interface.*.Username", { value: 1 });
if (deviceUsername.value !== undefined) {
  return { writable: true, value: deviceUsername.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdUsername = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username", { value: 1 });
if (igdUsername.value !== undefined) {
  return { writable: true, value: igdUsername.value };
}

return { writable: false, value: null };
