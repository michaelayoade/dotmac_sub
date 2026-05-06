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
 * TR-069 paths are device-specific and come from the DotMac ONT type registry.
 * No hardcoded paths - if no adapter is registered for a device model, config
 * is skipped with a warning.
 *
 * Per GenieACS docs: "Provision scripts may get executed multiple times
 * in a given session" - all operations here are idempotent.
 */

const now = Date.now();

// Detect data model root (TR-181 vs TR-098)
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
// Paths are device-specific from ONT type adapter registry.
// =============================================================================

/**
 * Safely set a TR-069 parameter.
 * Wraps declare() in try-catch to handle missing paths gracefully.
 */
function setParam(path, value) {
  if (!path || value === undefined || value === null) {
    return false;
  }
  try {
    declare(path, { value: now }, { value: value });
    return true;
  } catch (e) {
    log("  Warning: Failed to set " + path + ": " + e.message);
    return false;
  }
}

if (serial) {
  // Fetch full service config from DotMac API (includes paths from ONT type adapter)
  const config = ext("dotmac-webhook", "getServiceConfig", serial);

  if (!config) {
    log("No config returned for " + serial + " - device may not be registered");
  } else if (!config.paths) {
    log("No paths in config for " + serial + " - no ONT type adapter registered");
  } else {
    log("Restoring service config for " + serial + " using adapter: " + (config.adapter ? config.adapter.name : "unknown"));

    const paths = config.paths;

    // -------------------------------------------------------------------------
    // WiFi Configuration
    // -------------------------------------------------------------------------
    if (config.wifi && paths.wifi_ssid) {
      log("  WiFi: SSID=" + config.wifi.ssid);

      if (config.wifi.ssid) {
        setParam(paths.wifi_ssid, config.wifi.ssid);
      }
      if (config.wifi.password) {
        setParam(paths.wifi_password, config.wifi.password);
      }
      if (config.wifi.enabled !== undefined) {
        setParam(paths.wifi_enabled, config.wifi.enabled);
      }
      if (config.wifi.channel !== undefined && config.wifi.channel !== null) {
        setParam(paths.wifi_channel, config.wifi.channel);
      }
      if (config.wifi.security_mode_transformed) {
        // Use the device-specific transformed value
        setParam(paths.wifi_security_mode, config.wifi.security_mode_transformed);
      }

      // WiFi 5GHz (if dual-band and paths available)
      if (paths.wifi_5g_ssid && config.wifi.ssid_5g) {
        setParam(paths.wifi_5g_ssid, config.wifi.ssid_5g);
        setParam(paths.wifi_5g_password, config.wifi.password_5g || config.wifi.password);
        if (config.wifi.enabled_5g !== undefined) {
          setParam(paths.wifi_5g_enabled, config.wifi.enabled_5g);
        }
      }
    }

    // -------------------------------------------------------------------------
    // WAN Configuration (PPPoE credentials)
    // Note: WAN connection type is set via OMCI, we only set credentials here
    // -------------------------------------------------------------------------
    if (config.wan && config.wan.pppoe_username && paths.wan_pppoe_username) {
      log("  WAN: PPPoE user=" + config.wan.pppoe_username);

      setParam(paths.wan_pppoe_username, config.wan.pppoe_username);
      if (config.wan.pppoe_password) {
        setParam(paths.wan_pppoe_password, config.wan.pppoe_password);
      }
    }

    // -------------------------------------------------------------------------
    // LAN Configuration (IP, DHCP server)
    // -------------------------------------------------------------------------
    if (config.lan && paths.lan_ip_address) {
      log("  LAN: IP=" + (config.lan.ip || "default"));

      if (config.lan.ip) {
        setParam(paths.lan_ip_address, config.lan.ip);
      }
      if (config.lan.subnet) {
        setParam(paths.lan_subnet_mask, config.lan.subnet);
      }
      if (config.lan.dhcp_enabled !== undefined) {
        setParam(paths.lan_dhcp_enabled, config.lan.dhcp_enabled);
      }
      if (config.lan.dhcp_start) {
        setParam(paths.lan_dhcp_start, config.lan.dhcp_start);
      }
      if (config.lan.dhcp_end) {
        setParam(paths.lan_dhcp_end, config.lan.dhcp_end);
      }
    }

    // -------------------------------------------------------------------------
    // Access Control (Remote management, HTTP access)
    // -------------------------------------------------------------------------
    if (config.access) {
      log("  Access: http=" + config.access.http_management);

      if (config.access.http_management !== undefined && paths.http_management_enabled) {
        setParam(paths.http_management_enabled, config.access.http_management);
      }
      if (config.access.mgmt_remote !== undefined && paths.remote_access_enabled) {
        setParam(paths.remote_access_enabled, config.access.mgmt_remote);
      }
    }

    log("Service config restored for " + serial);
  }
}

log("Bootstrap provision completed for " + (serial || "unknown"));
