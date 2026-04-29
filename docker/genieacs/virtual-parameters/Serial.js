/**
 * Virtual Parameter: Serial
 * Returns the device serial number.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceSerial = declare("Device.DeviceInfo.SerialNumber", { value: 1 });
if (deviceSerial.value !== undefined) {
  return { writable: false, value: deviceSerial.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdSerial = declare("InternetGatewayDevice.DeviceInfo.SerialNumber", { value: 1 });
if (igdSerial.value !== undefined) {
  return { writable: false, value: igdSerial.value };
}

return { writable: false, value: null };
