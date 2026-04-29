/**
 * Virtual Parameter: WiFi_Enabled
 *
 * Normalizes WiFi enable/disable state across data models.
 * Writable - can be used to enable/disable WiFi.
 */

// Try TR-181 (Device) paths first
const deviceEnabled = declare(
  "Device.WiFi.Radio.1.Enable",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (deviceEnabled.value !== undefined) {
  return { writable: true, value: deviceEnabled.value };
}

// Fall back to TR-098 (InternetGatewayDevice) paths
const igdEnabled = declare(
  "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (igdEnabled.value !== undefined) {
  return { writable: true, value: igdEnabled.value };
}

// No enable state found
return { writable: false, value: [null, "xsd:boolean"] };
