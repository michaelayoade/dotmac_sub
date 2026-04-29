/**
 * Virtual Parameter: Telnet_Enabled
 * Returns whether Telnet remote access is enabled.
 * Huawei vendor-specific parameter (X_HW_UserInterface.TelnetEnable).
 */

// Try Device root first
const deviceTelnet = declare("Device.X_HW_UserInterface.TelnetEnable", { value: 1 });
if (deviceTelnet.value !== undefined) {
  return { writable: true, value: deviceTelnet.value };
}

// Try InternetGatewayDevice root
const igdTelnet = declare("InternetGatewayDevice.X_HW_UserInterface.TelnetEnable", { value: 1 });
if (igdTelnet.value !== undefined) {
  return { writable: true, value: igdTelnet.value };
}

return { writable: false, value: null };
