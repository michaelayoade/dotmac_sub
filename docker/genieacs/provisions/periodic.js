/**
 * DotMac GenieACS Periodic Provision
 *
 * This provision runs on periodic inform (2 PERIODIC event) to:
 * 1. Refresh key device parameters via virtual parameters
 * 2. Notify DotMac via webhook with current state
 *
 * All operations are idempotent per GenieACS requirements.
 */

// Detect data model root (TR-181 vs TR-098)
const now = Date.now();
let root = "Device";

const igd = declare("InternetGatewayDevice.DeviceInfo.Manufacturer", { value: 1 });
if (igd.value !== undefined) {
  root = "InternetGatewayDevice";
}

// Get connection request URL for callbacks (used in webhook)
const connectionRequestURL = declare(root + ".ManagementServer.ConnectionRequestURL", { value: 1 });

// Declare ALL virtual parameters to populate them in device document.
// Virtual params handle TR-098/TR-181 differences internally.

// System parameters
const vpManufacturer = declare("VirtualParameters.Manufacturer", { value: 1 });
const vpModel = declare("VirtualParameters.Model", { value: 1 });
const vpFirmware = declare("VirtualParameters.Firmware", { value: 1 });
const vpHardware = declare("VirtualParameters.Hardware", { value: 1 });
const vpSerial = declare("VirtualParameters.Serial", { value: 1 });
const vpUptime = declare("VirtualParameters.Uptime", { value: 1 });
const vpCPUUsage = declare("VirtualParameters.CPU_Usage", { value: 1 });
const vpMemoryTotal = declare("VirtualParameters.Memory_Total", { value: 1 });
const vpMemoryFree = declare("VirtualParameters.Memory_Free", { value: 1 });
const vpMACAddress = declare("VirtualParameters.MAC_Address", { value: 1 });

// WAN parameters
const vpWanIP = declare("VirtualParameters.WAN_IP", { value: 1 });
const vpWanGateway = declare("VirtualParameters.WAN_Gateway", { value: 1 });
const vpWanStatus = declare("VirtualParameters.WAN_Status", { value: 1 });
const vpWanVLAN = declare("VirtualParameters.WAN_VLAN", { value: 1 });
const vpPPPoEUsername = declare("VirtualParameters.PPPoE_Username", { value: 1 });
const vpWanConnectionType = declare("VirtualParameters.WAN_Connection_Type", { value: 1 });
const vpWanUptime = declare("VirtualParameters.WAN_Uptime", { value: 1 });
const vpDNSServers = declare("VirtualParameters.DNS_Servers", { value: 1 });

// LAN parameters
const vpLanIP = declare("VirtualParameters.LAN_IP", { value: 1 });
const vpLanSubnet = declare("VirtualParameters.LAN_Subnet", { value: 1 });
const vpDHCPEnabled = declare("VirtualParameters.DHCP_Enabled", { value: 1 });
const vpDHCPStart = declare("VirtualParameters.DHCP_Start", { value: 1 });
const vpDHCPEnd = declare("VirtualParameters.DHCP_End", { value: 1 });
const vpConnectedHosts = declare("VirtualParameters.Connected_Hosts", { value: 1 });

// WiFi parameters
const vpWifiEnabled = declare("VirtualParameters.WiFi_Enabled", { value: 1 });
const vpWifiSSID = declare("VirtualParameters.WiFi_SSID", { value: 1 });
const vpWifiPassword = declare("VirtualParameters.WiFi_Password", { value: 1 });
const vpWifiChannel = declare("VirtualParameters.WiFi_Channel", { value: 1 });
const vpWifiSecurity = declare("VirtualParameters.WiFi_Security", { value: 1 });
const vpWifiStandard = declare("VirtualParameters.WiFi_Standard", { value: 1 });
const vpWifiClients = declare("VirtualParameters.WiFi_Clients", { value: 1 });

// Management/Remote access
const vpMgmtIP = declare("VirtualParameters.Mgmt_IP", { value: 1 });
const vpSSHEnabled = declare("VirtualParameters.SSH_Enabled", { value: 1 });
const vpTelnetEnabled = declare("VirtualParameters.Telnet_Enabled", { value: 1 });

// Extract values for webhook
const wanIP = vpWanIP.value ? vpWanIP.value[0] : null;
const serial = vpSerial.value ? vpSerial.value[0] : null;
const manufacturer = vpManufacturer.value ? vpManufacturer.value[0] : null;
const firmware = vpFirmware.value ? vpFirmware.value[0] : null;
const uptime = vpUptime.value ? vpUptime.value[0] : null;

// Notify DotMac webhook about periodic inform
const deviceId = declare("DeviceID.ID", { value: 1 });
const oui = declare("DeviceID.OUI", { value: 1 });
const productClass = declare("DeviceID.ProductClass", { value: 1 });

try {
  ext(
    "dotmac-webhook",
    "informWebhook",
    deviceId.value ? deviceId.value[0] : "",
    serial || "",
    "periodic",
    oui.value ? oui.value[0] : "",
    productClass.value ? productClass.value[0] : "",
    JSON.stringify({
      manufacturer: manufacturer,
      software_version: firmware,
      uptime: uptime,
      wan_ip: wanIP,
      connection_request_url: connectionRequestURL.value ? connectionRequestURL.value[0] : null,
      data_model: root,
    })
  );
} catch (e) {
  // Don't fail provision if webhook fails
  log("Periodic webhook error: " + e.message);
}
