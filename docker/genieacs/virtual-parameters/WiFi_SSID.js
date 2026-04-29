/**
 * Virtual Parameter: WiFi_SSID
 *
 * Normalizes WiFi SSID across TR-098 (InternetGatewayDevice) and
 * TR-181 (Device) data models. Returns the primary SSID.
 *
 * Readable and writable - writing sets the SSID on the appropriate path.
 */

// Try TR-181 (Device) paths first
const deviceSSID = declare(
  "Device.WiFi.SSID.1.SSID",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (deviceSSID.value !== undefined) {
  return { writable: true, value: deviceSSID.value };
}

// Fall back to TR-098 (InternetGatewayDevice) paths
const igdSSID = declare(
  "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (igdSSID.value !== undefined) {
  return { writable: true, value: igdSSID.value };
}

// No SSID found
return { writable: false, value: [null, "xsd:string"] };
