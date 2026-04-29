/**
 * Virtual Parameter: WAN_Gateway
 *
 * Normalizes WAN gateway address across different data models.
 * Read-only - gateway is typically assigned by ISP.
 */

let gateway = null;

// Try TR-181 (Device) paths first
const deviceGW = declare("Device.IP.Interface.*.IPv4Address.*.Gateway", { value: 1 });
if (deviceGW.value !== undefined && deviceGW.value[0]) {
  gateway = deviceGW.value[0];
}

// Try Device routing table
if (!gateway) {
  const deviceRoute = declare("Device.Routing.Router.1.IPv4Forwarding.*.GatewayIPAddress", { value: 1 });
  if (deviceRoute.value !== undefined && deviceRoute.value[0]) {
    gateway = deviceRoute.value[0];
  }
}

// Fall back to TR-098 (InternetGatewayDevice) paths
if (!gateway) {
  // Try PPP connection
  const igdPPP = declare(
    "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.DefaultGateway",
    { value: 1 }
  );
  if (igdPPP.value !== undefined && igdPPP.value[0]) {
    gateway = igdPPP.value[0];
  }
}

if (!gateway) {
  // Try IP connection
  const igdIP = declare(
    "InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.DefaultGateway",
    { value: 1 }
  );
  if (igdIP.value !== undefined && igdIP.value[0]) {
    gateway = igdIP.value[0];
  }
}

return { writable: false, value: [gateway, "xsd:string"] };
