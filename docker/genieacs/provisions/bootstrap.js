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

function collectParamValues(path, attrName, candidates) {
  try {
    const result = declare(path, { path: now, value: now });
    for (let item of result) {
      if (!item.path || item.value === undefined || item.value === null) {
        continue;
      }
      const base = item.path.replace(/\.[^.]+$/, "");
      if (!candidates[base]) {
        candidates[base] = { base: base };
      }
      candidates[base][attrName] = item.value[0];
    }
  } catch (e) {
    log("  Warning: Failed to inspect " + path + ": " + e.message);
  }
}

function collectObjectPaths(path, candidates) {
  try {
    const result = declare(path, { path: now });
    for (let item of result) {
      if (!item.path) {
        continue;
      }
      if (!candidates[item.path]) {
        candidates[item.path] = { base: item.path };
      }
    }
  } catch (e) {
    log("  Warning: Failed to inspect " + path + ": " + e.message);
  }
}

function inferIgdPppBase(path) {
  if (!path) {
    return null;
  }
  const match = path.match(/^(InternetGatewayDevice\.WANDevice\.\d+\.WANConnectionDevice\.\d+\.WANPPPConnection\.\d+)\.[^.]+$/);
  return match ? match[1] : null;
}

function inferIgdPppCreatePath(paths, wanConfig) {
  const usernameBase = inferIgdPppBase(paths.wan_pppoe_username);
  if (usernameBase) {
    return usernameBase.replace(/\.WANPPPConnection\.\d+$/, ".WANPPPConnection.*");
  }

  const wcdIndex = wanConfig && wanConfig.wcd_index ? wanConfig.wcd_index : 1;
  return "InternetGatewayDevice.WANDevice.1.WANConnectionDevice." + wcdIndex + ".WANPPPConnection.*";
}

function scoreIgdPppCandidate(candidate, desiredUsername, desiredVlan, preferredBase) {
  let score = 0;
  const username = candidate.username === undefined || candidate.username === null ? "" : String(candidate.username);
  const serviceList = candidate.service_list === undefined || candidate.service_list === null ? "" : String(candidate.service_list).toUpperCase();
  const name = candidate.name === undefined || candidate.name === null ? "" : String(candidate.name).toUpperCase();
  const vlan = candidate.vlan === undefined || candidate.vlan === null ? "" : String(candidate.vlan);

  if (desiredUsername && username === String(desiredUsername)) {
    score += 100;
  }
  if (serviceList.indexOf("INTERNET") !== -1) {
    score += 50;
  }
  if (desiredVlan !== undefined && desiredVlan !== null && vlan === String(desiredVlan)) {
    score += 40;
  }
  if (name.indexOf("INTERNET") !== -1) {
    score += 20;
  }
  if (!username) {
    score += 10;
  }
  if (preferredBase && candidate.base === preferredBase) {
    score += 5;
  }

  return score;
}

function collectIgdPppCandidates() {
  const candidates = {};

  try {
    declare("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.*", { path: now });
  } catch (e) {
    log("  Warning: Failed to refresh WANPPPConnection tree: " + e.message);
  }

  collectObjectPaths("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*", candidates);
  collectParamValues("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Username", "username", candidates);
  collectParamValues("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.X_HW_SERVICELIST", "service_list", candidates);
  collectParamValues("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.X_HW_VLAN", "vlan", candidates);
  collectParamValues("InternetGatewayDevice.WANDevice.*.WANConnectionDevice.*.WANPPPConnection.*.Name", "name", candidates);

  const result = [];
  for (let base in candidates) {
    result.push(candidates[base]);
  }
  return result;
}

function resolveIgdPppBase(paths, wanConfig) {
  const preferredBase = inferIgdPppBase(paths.wan_pppoe_username);
  const desiredUsername = wanConfig ? wanConfig.pppoe_username : null;
  const desiredVlan = wanConfig ? wanConfig.vlan : null;
  let candidates = collectIgdPppCandidates();

  if (!candidates.length) {
    const createPath = inferIgdPppCreatePath(paths, wanConfig);
    try {
      log("  WAN: creating PPP object at " + createPath);
      declare(createPath, null, { path: 1 });
      commit();
    } catch (e) {
      log("  Warning: Failed to create PPP object at " + createPath + ": " + e.message);
    }
    candidates = collectIgdPppCandidates();
  }

  if (!candidates.length) {
    log("  Warning: No WANPPPConnection instance discovered after create");
    return null;
  }

  candidates.sort(function(a, b) {
    return scoreIgdPppCandidate(b, desiredUsername, desiredVlan, preferredBase) -
      scoreIgdPppCandidate(a, desiredUsername, desiredVlan, preferredBase);
  });

  return candidates[0].base;
}

function setPppoeCredentials(paths, wanConfig) {
  if (!wanConfig || !wanConfig.pppoe_username || !paths.wan_pppoe_username) {
    return false;
  }

  log("  WAN: PPPoE user=" + wanConfig.pppoe_username);

  if (root === "InternetGatewayDevice" && paths.wan_pppoe_username.indexOf("WANPPPConnection.") !== -1) {
    const base = resolveIgdPppBase(paths, wanConfig);
    if (!base) {
      log("  Warning: Unable to resolve WANPPPConnection instance for PPPoE credentials");
      return false;
    }

    log("  WAN: PPP instance=" + base);
    setParam(base + ".Username", wanConfig.pppoe_username);
    if (wanConfig.pppoe_password) {
      setParam(base + ".Password", wanConfig.pppoe_password);
    }
    return true;
  }

  setParam(paths.wan_pppoe_username, wanConfig.pppoe_username);
  if (wanConfig.pppoe_password) {
    setParam(paths.wan_pppoe_password, wanConfig.pppoe_password);
  }
  return true;
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
    setPppoeCredentials(paths, config.wan);

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
