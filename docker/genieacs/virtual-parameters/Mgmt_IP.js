/**
 * Virtual Parameter: Mgmt_IP
 * Extracts the management IP from ConnectionRequestURL.
 * The URL format is typically: http://IP:PORT/path
 */

// ConnectionRequestURL is the same path for both data models
let root = "Device";
const igd = declare("InternetGatewayDevice.DeviceInfo.Manufacturer", { value: 1 });
if (igd.value !== undefined) {
  root = "InternetGatewayDevice";
}

const crURL = declare(root + ".ManagementServer.ConnectionRequestURL", { value: 1 });
if (crURL.value !== undefined && crURL.value[0]) {
  const url = String(crURL.value[0]);
  // Extract IP from URL like "http://192.168.1.1:7547/..."
  const match = url.match(/https?:\/\/([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)/);
  if (match && match[1]) {
    return { writable: false, value: match[1] };
  }
  // Try IPv6 or hostname
  const match2 = url.match(/https?:\/\/\[?([^\]\/\:]+)\]?/);
  if (match2 && match2[1]) {
    return { writable: false, value: match2[1] };
  }
}

return { writable: false, value: null };
