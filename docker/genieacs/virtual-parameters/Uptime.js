/**
 * Virtual Parameter: Uptime
 * Returns the device uptime in seconds.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceUptime = declare("Device.DeviceInfo.UpTime", { value: 1 });
if (deviceUptime.value !== undefined) {
  return { writable: false, value: deviceUptime.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdUptime = declare("InternetGatewayDevice.DeviceInfo.UpTime", { value: 1 });
if (igdUptime.value !== undefined) {
  return { writable: false, value: igdUptime.value };
}

return { writable: false, value: null };
