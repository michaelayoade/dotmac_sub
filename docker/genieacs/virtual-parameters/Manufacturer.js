/**
 * Virtual Parameter: Manufacturer
 * Returns the device manufacturer.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceMfr = declare("Device.DeviceInfo.Manufacturer", { value: 1 });
if (deviceMfr.value !== undefined) {
  return { writable: false, value: deviceMfr.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdMfr = declare("InternetGatewayDevice.DeviceInfo.Manufacturer", { value: 1 });
if (igdMfr.value !== undefined) {
  return { writable: false, value: igdMfr.value };
}

return { writable: false, value: null };
