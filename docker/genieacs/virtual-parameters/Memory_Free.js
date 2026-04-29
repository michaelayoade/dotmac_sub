/**
 * Virtual Parameter: Memory_Free
 * Returns the device free memory in KB.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceMem = declare("Device.DeviceInfo.MemoryStatus.Free", { value: 1 });
if (deviceMem.value !== undefined) {
  return { writable: false, value: deviceMem.value };
}

// Try TR-098 (InternetGatewayDevice) path
const igdMem = declare("InternetGatewayDevice.DeviceInfo.MemoryStatus.Free", { value: 1 });
if (igdMem.value !== undefined) {
  return { writable: false, value: igdMem.value };
}

return { writable: false, value: null };
