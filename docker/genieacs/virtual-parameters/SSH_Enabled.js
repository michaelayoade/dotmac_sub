/**
 * Virtual Parameter: SSH_Enabled
 * Returns whether SSH remote access is enabled.
 * Huawei vendor-specific parameter (X_HW_UserInterface.SSHEnable).
 */

// Try Device root first
const deviceSSH = declare("Device.X_HW_UserInterface.SSHEnable", { value: 1 });
if (deviceSSH.value !== undefined) {
  return { writable: true, value: deviceSSH.value };
}

// Try InternetGatewayDevice root
const igdSSH = declare("InternetGatewayDevice.X_HW_UserInterface.SSHEnable", { value: 1 });
if (igdSSH.value !== undefined) {
  return { writable: true, value: igdSSH.value };
}

return { writable: false, value: null };
