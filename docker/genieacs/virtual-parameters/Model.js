/**
 * Virtual Parameter: Model
 * Returns the device model name.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceModel = declare("Device.DeviceInfo.ModelName", { value: 1 });
if (deviceModel.value !== undefined) {
  return { writable: false, value: deviceModel.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdModel = declare("InternetGatewayDevice.DeviceInfo.ModelName", { value: 1 });
if (igdModel.value !== undefined) {
  return { writable: false, value: igdModel.value };
}

return { writable: false, value: null };
