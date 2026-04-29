/**
 * Virtual Parameter: MAC_Address
 * Returns the device MAC address.
 * Tries WAN interface first, then LAN/Ethernet interfaces.
 */

// Try TR-098 WAN PPP MAC
const igdPPPMac = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.MACAddress", { value: 1 });
if (igdPPPMac.value !== undefined) {
  return { writable: false, value: igdPPPMac.value };
}

// Try TR-098 WAN IP MAC
const igdIPMac = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.MACAddress", { value: 1 });
if (igdIPMac.value !== undefined) {
  return { writable: false, value: igdIPMac.value };
}

// Try TR-181 Ethernet interface
const deviceEthMac = declare("Device.Ethernet.Interface.1.MACAddress", { value: 1 });
if (deviceEthMac.value !== undefined) {
  return { writable: false, value: deviceEthMac.value };
}

// Try TR-098 LAN Ethernet
const igdLanMac = declare("InternetGatewayDevice.LANDevice.1.LANEthernetInterfaceConfig.1.MACAddress", { value: 1 });
if (igdLanMac.value !== undefined) {
  return { writable: false, value: igdLanMac.value };
}

return { writable: false, value: null };
