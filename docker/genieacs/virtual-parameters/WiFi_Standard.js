/**
 * Virtual Parameter: WiFi_Standard
 * Returns the WiFi operating standard (a/b/g/n/ac/ax).
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceStd = declare("Device.WiFi.Radio.1.OperatingStandards", { value: 1 });
if (deviceStd.value !== undefined) {
  return { writable: true, value: deviceStd.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdStd = declare("InternetGatewayDevice.LANDevice.*.WLANConfiguration.*.Standard", { value: 1 });
if (igdStd.value !== undefined) {
  return { writable: true, value: igdStd.value };
}

const igdStdFixed = declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Standard", { value: 1 });
if (igdStdFixed.value !== undefined) {
  return { writable: true, value: igdStdFixed.value };
}

return { writable: false, value: null };
