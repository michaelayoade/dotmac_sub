/**
 * GenieACS Connection Request Authentication Extension
 * Provides digest authentication credentials for connection requests
 *
 * Environment variables:
 *   GENIEACS_CWMP_CONNECTION_REQUEST_USERNAME - Username for connection requests
 *   GENIEACS_CWMP_CONNECTION_REQUEST_PASSWORD - Password for connection requests
 */

const CONNECTION_REQUEST_USERNAME =
  process.env.GENIEACS_CWMP_CONNECTION_REQUEST_USERNAME || "acs";
const CONNECTION_REQUEST_PASSWORD =
  process.env.GENIEACS_CWMP_CONNECTION_REQUEST_PASSWORD || "changeme";

exports.connectionRequest = function (args, callback) {
  // args[0] is the device ID
  const deviceId = args[0] || "";

  // Return credentials for connection request from environment
  callback(null, {
    username: CONNECTION_REQUEST_USERNAME,
    password: CONNECTION_REQUEST_PASSWORD,
  });
};
