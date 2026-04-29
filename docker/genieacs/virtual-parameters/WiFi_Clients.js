/**
 * Virtual Parameter: WiFi_Clients
 * Returns the number of connected WiFi clients.
 * Normalizes TR-098 and TR-181 paths.
 */

// Try TR-181 (Device) path first
const deviceClients = declare("Device.WiFi.AccessPoint.1.AssociatedDeviceNumberOfEntries", { value: 1 });
if (deviceClients.value !== undefined) {
  return { writable: false, value: deviceClients.value };
}

// Try TR-098 (InternetGatewayDevice) paths
const igdClients = declare("InternetGatewayDevice.LANDevice.*.WLANConfiguration.*.TotalAssociations", { value: 1 });
if (igdClients.value !== undefined) {
  return { writable: false, value: igdClients.value };
}

const igdClientsFixed = declare("InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.TotalAssociations", { value: 1 });
if (igdClientsFixed.value !== undefined) {
  return { writable: false, value: igdClientsFixed.value };
}

return { writable: false, value: null };
