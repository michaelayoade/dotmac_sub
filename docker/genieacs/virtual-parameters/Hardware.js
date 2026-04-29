/**
 * Virtual Parameter: Hardware
 * Returns the device hardware version.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceHw = declare("Device.DeviceInfo.HardwareVersion", { value: 1 });
if (deviceHw.value !== undefined) {
  return { writable: false, value: deviceHw.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdHw = declare("InternetGatewayDevice.DeviceInfo.HardwareVersion", { value: 1 });
if (igdHw.value !== undefined) {
  return { writable: false, value: igdHw.value };
}

return { writable: false, value: null };
