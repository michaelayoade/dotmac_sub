/**
 * Virtual Parameter: WAN_IP
 *
 * Normalizes WAN IP address across different data models and connection types.
 * Supports PPPoE, DHCP, and static IP configurations.
 *
 * Read-only - WAN IP is typically assigned by ISP or DHCP.
 */

let wanIP = null;

// Try TR-181 (Device) paths first
const deviceIPv4 = declare("Device.IP.Interface.*.IPv4Address.*.IPAddress", { value: 1 });
if (deviceIPv4.value !== undefined && deviceIPv4.value[0]) {
  // Filter out private/link-local addresses to get WAN IP
  const ip = deviceIPv4.value[0];
  if (!ip.startsWith("192.168.") && !ip.startsWith("10.") && !ip.startsWith("169.254.")) {
    wanIP = ip;
  }
}

// Try Device PPP interface
if (!wanIP) {
  const devicePPP = declare("Device.PPP.Interface.*.IPCP.LocalIPAddress", { value: 1 });
  if (devicePPP.value !== undefined && devicePPP.value[0]) {
    wanIP = devicePPP.value[0];
  }
}

// Fall back to TR-098 (InternetGatewayDevice) paths
if (!wanIP) {
  // Try PPP connection
  const igdPPP = declare(
    "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.ExternalIPAddress",
    { value: 1 }
  );
  if (igdPPP.value !== undefined && igdPPP.value[0]) {
    wanIP = igdPPP.value[0];
  }
}

if (!wanIP) {
  // Try IP connection
  const igdIP = declare(
    "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.ExternalIPAddress",
    { value: 1 }
  );
  if (igdIP.value !== undefined && igdIP.value[0]) {
    wanIP = igdIP.value[0];
  }
}

return { writable: false, value: [wanIP, "xsd:string"] };
