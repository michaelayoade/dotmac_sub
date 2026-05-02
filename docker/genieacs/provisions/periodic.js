/**
 * DotMac GenieACS Periodic Provision
 *
 * This provision runs on periodic inform (2 PERIODIC event) to:
 * 1. Mirror the inform heartbeat into DotMac
 *
 * Keep this path intentionally small. Broad inventory refreshes and config
 * enforcement belong on BOOTSTRAP/BOOT, explicit tasks, or slower scheduled
 * refreshes so periodic informs stay cheap at fleet scale.
 */

// Detect data model root (TR-181 vs TR-098)
let root = "Device";

const igd = declare("InternetGatewayDevice.DeviceInfo.Manufacturer", { value: 1 });
if (igd.value !== undefined) {
  root = "InternetGatewayDevice";
}

// Get connection request URL for callbacks (used in webhook)
const connectionRequestURL = declare(root + ".ManagementServer.ConnectionRequestURL", { value: 1 });

const serialNumber = declare(root + ".DeviceInfo.SerialNumber", { value: 1 });
const manufacturer = declare(root + ".DeviceInfo.Manufacturer", { value: 1 });
const softwareVersion = declare(root + ".DeviceInfo.SoftwareVersion", { value: 1 });
const serial = serialNumber.value ? serialNumber.value[0] : null;

// Notify DotMac webhook about periodic inform
const deviceId = declare("DeviceID.ID", { value: 1 });
const oui = declare("DeviceID.OUI", { value: 1 });
const productClass = declare("DeviceID.ProductClass", { value: 1 });
const genieDeviceId = deviceId.value ? deviceId.value[0] : "";

try {
  ext(
    "dotmac-webhook",
    "informWebhook",
    genieDeviceId,
    serial || "",
    "periodic",
    oui.value ? oui.value[0] : "",
    productClass.value ? productClass.value[0] : "",
    JSON.stringify({
      manufacturer: manufacturer.value ? manufacturer.value[0] : null,
      software_version: softwareVersion.value ? softwareVersion.value[0] : null,
      connection_request_url: connectionRequestURL.value ? connectionRequestURL.value[0] : null,
      data_model: root,
    })
  );
} catch (e) {
  // Don't fail provision if webhook fails
  log("Periodic webhook error: " + e.message);
}
