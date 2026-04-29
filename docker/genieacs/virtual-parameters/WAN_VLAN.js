/**
 * Virtual Parameter: WAN_VLAN
 * Returns the WAN VLAN ID.
 * Normalizes TR-098 (X_HW_VLAN) and TR-181 (VLANTermination) paths.
 */

// Try TR-181 (Device) VLAN Termination
const deviceVLAN = declare("Device.Ethernet.VLANTermination.*.VLANID", { value: 1 });
if (deviceVLAN.value !== undefined) {
  return { writable: true, value: deviceVLAN.value };
}

// Try TR-098 (InternetGatewayDevice) Huawei-specific X_HW_VLAN
const igdPPPVLAN = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.X_HW_VLAN", { value: 1 });
if (igdPPPVLAN.value !== undefined) {
  return { writable: true, value: igdPPPVLAN.value };
}

const igdIPVLAN = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.X_HW_VLAN", { value: 1 });
if (igdIPVLAN.value !== undefined) {
  return { writable: true, value: igdIPVLAN.value };
}

return { writable: false, value: null };
