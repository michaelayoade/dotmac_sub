/**
 * GenieACS Connection Request Authentication Extension
 * Provides digest authentication credentials for connection requests
 */

exports.connectionRequest = function(args, callback) {
  // args[0] is the device ID
  const deviceId = args[0] || "";
  
  // Return credentials for connection request
  callback(null, {
    username: "acs_dotmac",
    password: "BJPZugdhdtHsO6tnoVI5XHjz"
  });
};
