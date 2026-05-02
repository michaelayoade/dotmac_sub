/**
 * GenieACS Authentication Extension
 *
 * Provides authentication for:
 * 1. CPE devices connecting to the ACS (inbound authentication)
 * 2. Connection requests from ACS to CPE (outbound authentication)
 *
 * Both support per-device credentials via DotMac API lookup with
 * fallback to environment variable defaults.
 *
 * Environment variables:
 *   DOTMAC_AUTH_URL - DotMac API base URL for credential lookups
 *   TR069_AUTH_SHARED_SECRET - Shared secret sent to DotMac auth endpoint
 *   GENIEACS_CWMP_CONNECTION_REQUEST_USERNAME - Default CR username
 *   GENIEACS_CWMP_CONNECTION_REQUEST_PASSWORD - Default CR password
 *   GENIEACS_CPE_AUTH_USERNAME - Bootstrap/default CPE auth username
 *   GENIEACS_CPE_AUTH_PASSWORD - Bootstrap/default CPE auth password
 */

const http = require("http");
const https = require("https");

// Configuration from environment
const DOTMAC_AUTH_URL = process.env.DOTMAC_AUTH_URL || "http://app:8001/api/v1/tr069/auth";
const TR069_AUTH_SHARED_SECRET = process.env.TR069_AUTH_SHARED_SECRET || "";
const DEFAULT_CR_USERNAME = process.env.GENIEACS_CWMP_CONNECTION_REQUEST_USERNAME || "acs";
const DEFAULT_CR_PASSWORD = process.env.GENIEACS_CWMP_CONNECTION_REQUEST_PASSWORD || "acs123";
const DEFAULT_CPE_USERNAME = process.env.GENIEACS_CPE_AUTH_USERNAME || "";
const DEFAULT_CPE_PASSWORD = process.env.GENIEACS_CPE_AUTH_PASSWORD || "";

// Cache for credentials (TTL: 5 minutes)
const credentialCache = new Map();
const CACHE_TTL_MS = 5 * 60 * 1000;

/**
 * Parse URL into components
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
 * Fetch credentials from DotMac API
 */
function fetchCredentials(serialNumber, credentialType, callback) {
  const cacheKey = `${credentialType}:${serialNumber}`;
  const cached = credentialCache.get(cacheKey);

  if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
    callback(null, cached.credentials);
    return;
  }

  const url = `${DOTMAC_AUTH_URL}?serial_number=${encodeURIComponent(serialNumber)}&type=${credentialType}`;
  const urlParts = parseUrl(url);
  const httpModule = urlParts.protocol === "https:" ? https : http;

  const req = httpModule.get(
    {
      hostname: urlParts.hostname,
      port: urlParts.port,
      path: urlParts.path,
      timeout: 3000,
      headers: {
        "Accept": "application/json",
        "User-Agent": "GenieACS-Auth-Extension/1.0",
        "X-DotMac-TR069-Auth": TR069_AUTH_SHARED_SECRET,
      },
    },
    (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        if (res.statusCode === 200) {
          try {
            const data = JSON.parse(body);
            const credentials = {
              username: data.username || null,
              password: data.password || null,
              authorized: data.authorized === true,
            };
            // Cache successful lookups
            credentialCache.set(cacheKey, {
              credentials: credentials,
              timestamp: Date.now(),
            });
            callback(null, credentials);
          } catch (e) {
            callback(new Error("Invalid JSON response"));
          }
        } else if (res.statusCode === 404) {
          // Device not found - use defaults
          callback(null, null);
        } else {
          callback(new Error(`HTTP ${res.statusCode}`));
        }
      });
    }
  );

  req.on("error", (err) => {
    callback(err);
  });

  req.on("timeout", () => {
    req.destroy();
    callback(new Error("Request timeout"));
  });
}

/**
 * Get connection request credentials for a device
 *
 * Called by GenieACS when sending connection requests to CPE devices.
 * Returns username/password for digest authentication.
 *
 * Args from provision:
 *   args[0] - device ID (GenieACS _id)
 *   args[1] - serial number (optional)
 */
exports.connectionRequest = function (args, callback) {
  const deviceId = args[0] || "";
  const serialNumber = args[1] || "";

  // If serial provided, try per-device lookup
  if (serialNumber) {
    fetchCredentials(serialNumber, "connection_request", (err, creds) => {
      if (err) {
        console.error(`CR credential lookup failed for ${serialNumber}: ${err.message}`);
      }

      if (creds && creds.authorized && creds.username && creds.password) {
        callback(null, creds);
      } else {
        // Fallback to defaults
        callback(null, {
          username: DEFAULT_CR_USERNAME,
          password: DEFAULT_CR_PASSWORD,
        });
      }
    });
  } else {
    // No serial, use defaults
    callback(null, {
      username: DEFAULT_CR_USERNAME,
      password: DEFAULT_CR_PASSWORD,
    });
  }
};

/**
 * Authenticate CPE device connecting to ACS
 *
 * Called by GenieACS to verify inbound CPE connections.
 * Returns true if credentials are valid, false otherwise.
 *
 * Args from cwmp.auth config:
 *   args[0] - username from CPE
 *   args[1] - password from CPE
 *   args[2] - device ID (GenieACS _id)
 *   args[3] - serial number
 */
exports.authenticateCpe = function (args, callback) {
  const providedUsername = args[0] || "";
  const providedPassword = args[1] || "";
  const deviceId = args[2] || "";
  const serialNumber = args[3] || "";

  function checkCredentials(expectedUsername, expectedPassword) {
    if (!expectedUsername || !expectedPassword) {
      callback(null, false);
      return;
    }

    callback(
      null,
      providedUsername === expectedUsername && providedPassword === expectedPassword
    );
  }

  // Prefer per-device credentials. Fall back only to explicit bootstrap/default
  // credentials; blank defaults must fail closed.
  if (serialNumber) {
    fetchCredentials(serialNumber, "cpe_auth", (err, creds) => {
      if (err) {
        console.error(`CPE auth lookup failed for ${serialNumber}: ${err.message}`);
      }

      if (creds && creds.username && creds.password) {
        checkCredentials(creds.username, creds.password);
        return;
      }

      if (creds && creds.authorized) {
        checkCredentials(DEFAULT_CPE_USERNAME, DEFAULT_CPE_PASSWORD);
        return;
      }

      callback(null, false);
    });
  } else {
    callback(null, false);
  }
};

/**
 * Get CPE authentication credentials for a device
 *
 * Returns the expected credentials that a CPE should use when connecting.
 * Used by provisions to set ManagementServer.Username/Password on device.
 *
 * Args:
 *   args[0] - serial number
 */
exports.getCpeCredentials = function (args, callback) {
  const serialNumber = args[0] || "";

  if (serialNumber) {
    fetchCredentials(serialNumber, "cpe_auth", (err, creds) => {
      if (err) {
        console.error(`CPE credential lookup failed for ${serialNumber}: ${err.message}`);
      }

      if (creds && creds.authorized && creds.username && creds.password) {
        callback(null, creds);
      } else if (creds && creds.authorized) {
        callback(null, {
          username: DEFAULT_CPE_USERNAME,
          password: DEFAULT_CPE_PASSWORD,
        });
      } else {
        callback(null, { username: null, password: null });
      }
    });
  } else {
    callback(null, { username: null, password: null });
  }
};

/**
 * Clear credential cache for a device
 *
 * Call this after updating credentials in DotMac to force refresh.
 *
 * Args:
 *   args[0] - serial number
 */
exports.clearCache = function (args, callback) {
  const serialNumber = args[0] || "";

  if (serialNumber) {
    credentialCache.delete(`connection_request:${serialNumber}`);
    credentialCache.delete(`cpe_auth:${serialNumber}`);
    callback(null, { cleared: true, serial: serialNumber });
  } else {
    // Clear all
    credentialCache.clear();
    callback(null, { cleared: true, all: true });
  }
};
