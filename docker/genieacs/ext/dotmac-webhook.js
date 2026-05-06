/**
 * DotMac GenieACS Extension - Inform Webhook
 *
 * This extension sends device inform data to the DotMac application
 * for logging and processing. Called from GenieACS provisions.
 */

const http = require("http");
const https = require("https");

// Configuration - use environment variable or default to Docker network hostname
const WEBHOOK_URL = process.env.DOTMAC_WEBHOOK_URL || "http://app:8000/api/v1/tr069/inform";

/**
 * Parse URL and return components
 */
function parseUrl(url) {
  const parsed = new URL(url);
  return {
    protocol: parsed.protocol,
    hostname: parsed.hostname,
    port: parsed.port || (parsed.protocol === "https:" ? 443 : 80),
    path: parsed.pathname + parsed.search,
  };
}

/**
 * Send HTTP POST request with JSON payload
 */
function postJson(url, data, callback) {
  const urlParts = parseUrl(url);
  const jsonData = JSON.stringify(data);

  const options = {
    hostname: urlParts.hostname,
    port: urlParts.port,
    path: urlParts.path,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(jsonData),
      "User-Agent": "GenieACS-DotMac-Extension/1.0",
    },
    timeout: 10000,
  };

  const httpModule = urlParts.protocol === "https:" ? https : http;

  const req = httpModule.request(options, (res) => {
    let body = "";
    res.on("data", (chunk) => {
      body += chunk;
    });
    res.on("end", () => {
      callback(null, { statusCode: res.statusCode, body: body });
    });
  });

  req.on("error", (err) => {
    callback(err);
  });

  req.on("timeout", () => {
    req.destroy();
    callback(new Error("Request timeout"));
  });

  req.write(jsonData);
  req.end();
}

/**
 * Main extension function - called from GenieACS provisions
 *
 * Arguments from provision:
 *   args[0] - device_id (GenieACS _id)
 *   args[1] - serial_number
 *   args[2] - event type (boot, periodic, etc.)
 *   args[3] - OUI
 *   args[4] - product_class
 *   args[5] - additional parameters (JSON string, optional)
 */
exports.informWebhook = function (args, callback) {
  const deviceId = args[0] || null;
  const serialNumber = args[1] || null;
  const event = args[2] || "periodic";
  const oui = args[3] || null;
  const productClass = args[4] || null;
  const extraParams = args[5] ? JSON.parse(args[5]) : {};

  const payload = {
    device_id: deviceId,
    serial_number: serialNumber,
    event: event,
    oui: oui,
    product_class: productClass,
    timestamp: new Date().toISOString(),
    source: "genieacs-extension",
    ...extraParams,
  };

  postJson(WEBHOOK_URL, payload, (err, response) => {
    if (err) {
      // Log error but don't fail the provision
      console.error(`DotMac webhook error: ${err.message}`);
      callback(null, { success: false, error: err.message });
      return;
    }

    if (response.statusCode >= 200 && response.statusCode < 300) {
      callback(null, { success: true, status: response.statusCode });
    } else {
      console.error(`DotMac webhook HTTP ${response.statusCode}: ${response.body}`);
      callback(null, { success: false, status: response.statusCode, body: response.body });
    }
  });
};

/**
 * Get service configuration for a device from DotMac
 *
 * Called from bootstrap provision to restore TR-069 config after ONT reboot.
 * TR-069 config is volatile - lost on reboot. OMCI config (mgmt IP, VLANs)
 * persists on OLT. This fetches TR-069-only settings:
 *   - WiFi (SSID, password, channel, security)
 *   - WAN (PPPoE credentials, DHCP/static mode)
 *   - LAN (IP, subnet, DHCP server)
 *   - Access (remote management, HTTP)
 *
 * Arguments from provision:
 *   args[0] - serial_number
 *
 * Returns full config object or null if device not found:
 *   { wifi: {...}, wan: {...}, lan: {...}, access: {...} }
 */
exports.getServiceConfig = function (args, callback) {
  const serialNumber = args[0];

  if (!serialNumber) {
    callback(null, null);
    return;
  }

  const configUrl = (process.env.DOTMAC_WEBHOOK_URL || "http://app:8000/api/v1/tr069/inform")
    .replace("/inform", "/device-config/" + encodeURIComponent(serialNumber));

  const urlParts = parseUrl(configUrl);
  const httpModule = urlParts.protocol === "https:" ? https : http;

  const req = httpModule.get(
    {
      hostname: urlParts.hostname,
      port: urlParts.port,
      path: urlParts.path,
      headers: {
        "User-Agent": "GenieACS-DotMac-Extension/1.0",
        "Accept": "application/json",
      },
      timeout: 10000,
    },
    (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        if (res.statusCode === 200) {
          try {
            const config = JSON.parse(body);
            callback(null, config);
          } catch (e) {
            console.error("Failed to parse service config response: " + e.message);
            callback(null, null);
          }
        } else if (res.statusCode === 404) {
          // Device not found or no config - this is normal
          callback(null, null);
        } else {
          console.error("Service config fetch HTTP " + res.statusCode + ": " + body);
          callback(null, null);
        }
      });
    }
  );

  req.on("error", (err) => {
    console.error("Service config fetch error: " + err.message);
    callback(null, null);
  });

  req.on("timeout", () => {
    req.destroy();
    console.error("Service config fetch timeout");
    callback(null, null);
  });
};

/**
 * Health check function - verify webhook endpoint is reachable
 */
exports.healthCheck = function (args, callback) {
  const url = args[0] || WEBHOOK_URL.replace("/inform", "/health");

  const urlParts = parseUrl(url);
  const httpModule = urlParts.protocol === "https:" ? https : http;

  const req = httpModule.get(
    {
      hostname: urlParts.hostname,
      port: urlParts.port,
      path: urlParts.path,
      timeout: 5000,
    },
    (res) => {
      callback(null, { reachable: true, statusCode: res.statusCode });
    }
  );

  req.on("error", (err) => {
    callback(null, { reachable: false, error: err.message });
  });

  req.on("timeout", () => {
    req.destroy();
    callback(null, { reachable: false, error: "timeout" });
  });
};
