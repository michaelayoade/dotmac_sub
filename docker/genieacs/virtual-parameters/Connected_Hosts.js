/**
 * Virtual Parameter: Connected_Hosts
 * Returns the number of connected LAN hosts.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceHosts = declare("Device.Hosts.HostNumberOfEntries", { value: 1 });
if (deviceHosts.value !== undefined) {
  return { writable: false, value: deviceHosts.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdHosts = declare("InternetGatewayDevice.LANDevice.*.Hosts.HostNumberOfEntries", { value: 1 });
if (igdHosts.value !== undefined) {
  return { writable: false, value: igdHosts.value };
}

const igdHostsFixed = declare("InternetGatewayDevice.LANDevice.1.Hosts.HostNumberOfEntries", { value: 1 });
if (igdHostsFixed.value !== undefined) {
  return { writable: false, value: igdHostsFixed.value };
}

return { writable: false, value: null };
