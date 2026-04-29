/**
 * Virtual Parameter: WiFi_Security
 * Returns the WiFi security mode (WPA2-Personal, etc.).
 * Normalizes TR-098 (BeaconType) and TR-181 (ModeEnabled) paths.
 */

// Try TR-181 (Device) path
const deviceSecurity = declare("Device.WiFi.AccessPoint.1.Security.ModeEnabled", { value: 1 });
if (deviceSecurity.value !== undefined) {
  return { writable: true, value: deviceSecurity.value };
}

// Try TR-098 (InternetGatewayDevice) paths - BeaconType
const igdSecurity = declare("InternetGatewayDevice.LANDevice.*.WLANConfiguration.*.BeaconType", { value: 1 });
if (igdSecurity.value !== undefined) {
  return { writable: true, value: igdSecurity.value };
}

const igdSecurityFixed = declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType", { value: 1 });
if (igdSecurityFixed.value !== undefined) {
  return { writable: true, value: igdSecurityFixed.value };
}

return { writable: false, value: null };
