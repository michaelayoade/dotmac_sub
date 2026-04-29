/**
 * Virtual Parameter: CPU_Usage
 * Returns the device CPU usage percentage.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path
const deviceCPU = declare("Device.DeviceInfo.ProcessStatus.CPUUsage", { value: 1 });
if (deviceCPU.value !== undefined) {
  return { writable: false, value: deviceCPU.value };
}

// TR-098 doesn't have a standard CPU usage path
return { writable: false, value: null };
