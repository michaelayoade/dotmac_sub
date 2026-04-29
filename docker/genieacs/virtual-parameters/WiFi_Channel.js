/**
 * Virtual Parameter: WiFi_Channel
 * Returns the WiFi channel number.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path
const deviceChannel = declare("Device.WiFi.Radio.1.Channel", { value: 1 });
if (deviceChannel.value !== undefined) {
  return { writable: true, value: deviceChannel.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdChannel = declare("InternetGatewayDevice.LANDevice.*.WLANConfiguration.*.Channel", { value: 1 });
if (igdChannel.value !== undefined) {
  return { writable: true, value: igdChannel.value };
}

const igdChannelFixed = declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Channel", { value: 1 });
if (igdChannelFixed.value !== undefined) {
  return { writable: true, value: igdChannelFixed.value };
}

return { writable: false, value: null };
