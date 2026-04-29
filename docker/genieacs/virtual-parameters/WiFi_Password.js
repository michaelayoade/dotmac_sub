/**
 * Virtual Parameter: WiFi_Password
 *
 * Normalizes WiFi password/passphrase across TR-098 and TR-181 data models.
 * Writable for setting WPA/WPA2 pre-shared key.
 *
 * Note: Reading passwords may return empty due to device security policies.
 */

// Try TR-181 (Device) paths first - WPA2 PSK
const devicePSK = declare(
  "Device.WiFi.AccessPoint.1.Security.KeyPassphrase",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (devicePSK.value !== undefined) {
  return { writable: true, value: devicePSK.value };
}

// Try alternative TR-181 path
const devicePreSharedKey = declare(
  "Device.WiFi.AccessPoint.1.Security.PreSharedKey",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (devicePreSharedKey.value !== undefined) {
  return { writable: true, value: devicePreSharedKey.value };
}

// Fall back to TR-098 (InternetGatewayDevice) paths
const igdPSK = declare(
  "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.KeyPassphrase",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (igdPSK.value !== undefined) {
  return { writable: true, value: igdPSK.value };
}

// Try alternative TR-098 path
const igdPreSharedKey = declare(
  "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (igdPreSharedKey.value !== undefined) {
  return { writable: true, value: igdPreSharedKey.value };
}

// No password found
return { writable: true, value: [null, "xsd:string"] };
