/**
 * Virtual Parameter: Firmware
 * Returns the device firmware/software version.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceFw = declare("Device.DeviceInfo.SoftwareVersion", { value: 1 });
if (deviceFw.value !== undefined) {
  return { writable: false, value: deviceFw.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdFw = declare("InternetGatewayDevice.DeviceInfo.SoftwareVersion", { value: 1 });
if (igdFw.value !== undefined) {
  return { writable: false, value: igdFw.value };
}

return { writable: false, value: null };
