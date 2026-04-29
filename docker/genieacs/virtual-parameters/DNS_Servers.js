/**
 * Virtual Parameter: DNS_Servers
 * Returns the DNS servers configured on the WAN interface.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-098 PPP DNS
const igdPPP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.DNSServers", { value: 1 });
if (igdPPP.value !== undefined) {
  return { writable: false, value: igdPPP.value };
}

// Try TR-098 IP DNS
const igdIP = declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANIPConnection.*.DNSServers", { value: 1 });
if (igdIP.value !== undefined) {
  return { writable: false, value: igdIP.value };
}

// Try TR-181 DNS client
const deviceDNS = declare("Device.DNS.Client.Server.1.DNSServer", { value: 1 });
if (deviceDNS.value !== undefined) {
  return { writable: false, value: deviceDNS.value };
}

return { writable: false, value: null };
