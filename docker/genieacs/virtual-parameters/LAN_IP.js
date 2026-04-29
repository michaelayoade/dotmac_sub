/**
 * Virtual Parameter: LAN_IP
 *
 * Normalizes LAN IP address across data models.
 * Writable - can be used to change the device's LAN IP.
 */

// Try TR-181 (Device) paths first
const deviceLAN = declare(
  "Device.IP.Interface.1.IPv4Address.1.IPAddress",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (deviceLAN.value !== undefined) {
  // Check if this looks like a LAN IP (private range)
  const ip = deviceLAN.value[0];
  if (ip && (ip.startsWith("192.168.") || ip.startsWith("10.") || ip.startsWith("172."))) {
    return { writable: true, value: deviceLAN.value };
  }
}

// Try Device LANHostConfigManagement
const deviceLANConfig = declare(
  "Device.DHCPv4.Server.Pool.1.MinAddress",
  { value: 1 }
);

// Fall back to TR-098 (InternetGatewayDevice) paths
const igdLAN = declare(
  "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
  { value: args.value ? Date.now() : 1 },
  args.value ? { value: args.value[0] } : undefined
);

if (igdLAN.value !== undefined) {
  return { writable: true, value: igdLAN.value };
}

// No LAN IP found
return { writable: false, value: [null, "xsd:string"] };
