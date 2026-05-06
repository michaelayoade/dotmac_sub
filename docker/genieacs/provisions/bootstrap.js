/**
 * DotMac GenieACS Bootstrap Provision
 *
 * This provision runs on device bootstrap (0 BOOTSTRAP event) to:
 * 1. Clear cached data model after factory reset
 * 2. Configure periodic inform interval
 * 3. Restore TR-069 service config (WiFi, WAN, LAN, Access)
 *
 * TR-069 config is volatile - lost on ONT reboot. OMCI config (management IP,
 * VLANs, service ports) persists on OLT. This provision restores TR-069 settings.
 *
 * Per GenieACS docs: "Provision scripts may get executed multiple times
 * in a given session" - all operations here are idempotent.
 */

// Detect data model root (TR-181 vs TR-098)
const now = Date.now();
let root = "Device";

const igd = declare("InternetGatewayDevice.DeviceInfo.Manufacturer", { value: 1 });
if (igd.value !== undefined) {
  root = "InternetGatewayDevice";
}

// Clear cached data model to force fresh discovery after factory reset
// This is critical per GenieACS FAQ - without this, stale params are used
clear("Device", now);
clear("InternetGatewayDevice", now);

// Get device identification
const serialNumber = declare(root + ".DeviceInfo.SerialNumber", { value: 1 });
const serial = serialNumber.value ? serialNumber.value[0] : "";

// Get current management server settings
const periodicInformInterval = declare(root + ".ManagementServer.PeriodicInformInterval", { value: 1 });

// Set periodic inform interval to 5 minutes (300 seconds) if not already set
const targetInterval = 300;
if (periodicInformInterval.value === undefined || periodicInformInterval.value[0] !== targetInterval) {
  declare(root + ".ManagementServer.PeriodicInformInterval", { value: now }, { value: targetInterval });
}

// Enable periodic inform if disabled
const periodicInformEnable = declare(root + ".ManagementServer.PeriodicInformEnable", { value: 1 });
if (periodicInformEnable.value === undefined || periodicInformEnable.value[0] !== true) {
  declare(root + ".ManagementServer.PeriodicInformEnable", { value: now }, { value: true });
}

// =============================================================================
// Service Config Restore
// TR-069 config is volatile - fetch from DotMac and re-apply on every boot.
// OMCI handles: management IP, VLANs, service ports (persistent on OLT)
// TR-069 handles: WiFi, WAN credentials, LAN, access control (volatile)
// =============================================================================

if (serial) {
  // Fetch full service config from DotMac API
  const config = ext("dotmac-webhook", "getServiceConfig", serial);

  if (config) {
    log("Restoring service config for " + serial);

    // -------------------------------------------------------------------------
    // WiFi Configuration
    // -------------------------------------------------------------------------
    if (config.wifi && config.wifi.ssid) {
      log("  WiFi: SSID=" + config.wifi.ssid);

      let wifiSsidPath, wifiPskPath, wifiEnablePath, wifiChannelPath, wifiSecurityPath;

      if (root === "Device") {
        wifiSsidPath = "Device.WiFi.SSID.1.SSID";
        wifiPskPath = "Device.WiFi.AccessPoint.1.Security.KeyPassphrase";
        wifiEnablePath = "Device.WiFi.SSID.1.Enable";
        wifiChannelPath = "Device.WiFi.Radio.1.Channel";
        wifiSecurityPath = "Device.WiFi.AccessPoint.1.Security.ModeEnabled";
      } else {
        wifiSsidPath = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.SSID";
        wifiPskPath = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.PreSharedKey.1.PreSharedKey";
        wifiEnablePath = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Enable";
        wifiChannelPath = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.Channel";
        wifiSecurityPath = "InternetGatewayDevice.LANDevice.1.WLANConfiguration.1.BeaconType";
      }

      if (config.wifi.ssid) {
        declare(wifiSsidPath, { value: now }, { value: config.wifi.ssid });
      }
      if (config.wifi.password) {
        declare(wifiPskPath, { value: now }, { value: config.wifi.password });
      }
      if (config.wifi.enabled !== undefined) {
        declare(wifiEnablePath, { value: now }, { value: config.wifi.enabled });
      }
      if (config.wifi.channel !== undefined && config.wifi.channel !== null) {
        declare(wifiChannelPath, { value: now }, { value: config.wifi.channel });
      }
      if (config.wifi.security_mode) {
        let securityValue = config.wifi.security_mode;
        if (root !== "Device") {
          const securityMap = {
            "WPA2": "11i", "WPA2-Personal": "11i", "WPA2-PSK": "11i",
            "WPA": "WPA", "WPA-Personal": "WPA", "WPA-PSK": "WPA",
            "WPA+WPA2": "WPAand11i", "Mixed": "WPAand11i",
            "None": "None", "Open": "None",
          };
          securityValue = securityMap[config.wifi.security_mode] || config.wifi.security_mode;
        }
        declare(wifiSecurityPath, { value: now }, { value: securityValue });
      }
    }

    // -------------------------------------------------------------------------
    // WAN Configuration (PPPoE credentials)
    // Note: WAN connection type is set via OMCI, we only set credentials here
    // -------------------------------------------------------------------------
    if (config.wan && config.wan.pppoe_username) {
      log("  WAN: PPPoE user=" + config.wan.pppoe_username);

      let pppUserPath, pppPassPath;

      if (root === "Device") {
        // TR-181: PPP interface credentials
        pppUserPath = "Device.PPP.Interface.1.Username";
        pppPassPath = "Device.PPP.Interface.1.Password";
      } else {
        // TR-098: WANPPPConnection credentials
        pppUserPath = "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username";
        pppPassPath = "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password";
      }

      declare(pppUserPath, { value: now }, { value: config.wan.pppoe_username });
      if (config.wan.pppoe_password) {
        declare(pppPassPath, { value: now }, { value: config.wan.pppoe_password });
      }
    }

    // -------------------------------------------------------------------------
    // LAN Configuration (IP, DHCP server)
    // -------------------------------------------------------------------------
    if (config.lan && (config.lan.ip || config.lan.dhcp_enabled !== undefined)) {
      log("  LAN: IP=" + (config.lan.ip || "default"));

      let lanIpPath, lanSubnetPath, lanDhcpEnablePath, lanDhcpStartPath, lanDhcpEndPath;

      if (root === "Device") {
        lanIpPath = "Device.IP.Interface.1.IPv4Address.1.IPAddress";
        lanSubnetPath = "Device.IP.Interface.1.IPv4Address.1.SubnetMask";
        lanDhcpEnablePath = "Device.DHCPv4.Server.Enable";
        lanDhcpStartPath = "Device.DHCPv4.Server.Pool.1.MinAddress";
        lanDhcpEndPath = "Device.DHCPv4.Server.Pool.1.MaxAddress";
      } else {
        lanIpPath = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress";
        lanSubnetPath = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask";
        lanDhcpEnablePath = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.DHCPServerEnable";
        lanDhcpStartPath = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MinAddress";
        lanDhcpEndPath = "InternetGatewayDevice.LANDevice.1.LANHostConfigManagement.MaxAddress";
      }

      if (config.lan.ip) {
        declare(lanIpPath, { value: now }, { value: config.lan.ip });
      }
      if (config.lan.subnet) {
        declare(lanSubnetPath, { value: now }, { value: config.lan.subnet });
      }
      if (config.lan.dhcp_enabled !== undefined) {
        declare(lanDhcpEnablePath, { value: now }, { value: config.lan.dhcp_enabled });
      }
      if (config.lan.dhcp_start) {
        declare(lanDhcpStartPath, { value: now }, { value: config.lan.dhcp_start });
      }
      if (config.lan.dhcp_end) {
        declare(lanDhcpEndPath, { value: now }, { value: config.lan.dhcp_end });
      }
    }

    // -------------------------------------------------------------------------
    // Access Control (Remote management, HTTP access)
    // -------------------------------------------------------------------------
    if (config.access) {
      log("  Access: wan_remote=" + config.access.wan_remote + ", http=" + config.access.http_management);

      let remoteAccessPath, httpEnablePath;

      if (root === "Device") {
        // TR-181 paths vary by vendor - use common patterns
        remoteAccessPath = "Device.UserInterface.RemoteAccess.Enable";
        httpEnablePath = "Device.UserInterface.HTTPAccess.Enable";
      } else {
        // TR-098: Management server remote access
        remoteAccessPath = "InternetGatewayDevice.ManagementServer.EnableCWMP";
        httpEnablePath = "InternetGatewayDevice.UserInterface.RemoteAccess.Enable";
      }

      // Note: Remote access paths are vendor-specific, may need adjustment
      if (config.access.http_management !== undefined) {
        try {
          declare(httpEnablePath, { value: now }, { value: config.access.http_management });
        } catch (e) {
          // Path may not exist on all devices
        }
      }
    }

    log("Service config restored for " + serial);
  }
}

log("Bootstrap provision completed for " + (serial || "unknown"));
