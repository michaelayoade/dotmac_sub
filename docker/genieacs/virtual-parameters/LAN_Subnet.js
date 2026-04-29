/**
 * Virtual Parameter: LAN_Subnet
 * Returns the LAN subnet mask.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path
const deviceSubnet = declare("Device.IP.Interface.2.IPv4Address.1.SubnetMask", { value: 1 });
if (deviceSubnet.value !== undefined) {
  return { writable: true, value: deviceSubnet.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdSubnet = declare("InternetGatewayDevice.LANDevice.*.LANHostConfigManagement.IPInterface.*.IPInterfaceSubnetMask", { value: 1 });
if (igdSubnet.value !== undefined) {
  return { writable: true, value: igdSubnet.value };
}

const igdSubnetFixed = declare("InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask", { value: 1 });
if (igdSubnetFixed.value !== undefined) {
  return { writable: true, value: igdSubnetFixed.value };
}

return { writable: false, value: null };
