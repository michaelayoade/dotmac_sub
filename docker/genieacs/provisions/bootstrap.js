/**
 * DotMac GenieACS Bootstrap Provision
 *
 * This provision runs on device bootstrap (0 BOOTSTRAP event) to:
 * 1. Clear cached data model after factory reset
 * 2. Set management server credentials
 * 3. Configure periodic inform interval
 * 4. Notify DotMac via webhook
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
const manufacturer = declare(root + ".DeviceInfo.Manufacturer", { value: 1 });
const productClass = declare(root + ".DeviceInfo.ProductClass", { value: 1 });
const softwareVersion = declare(root + ".DeviceInfo.SoftwareVersion", { value: 1 });
const deviceId = declare("DeviceID.ID", { value: 1 });
const serial = serialNumber.value ? serialNumber.value[0] : "";
const genieDeviceId = deviceId.value ? deviceId.value[0] : "";

try {
  const crCredentials = ext("auth", "connectionRequest", genieDeviceId, serial);
  if (crCredentials && crCredentials.username && crCredentials.password) {
    declare(root + ".ManagementServer.ConnectionRequestUsername", { value: now }, { value: crCredentials.username });
    declare(root + ".ManagementServer.ConnectionRequestPassword", { value: now }, { value: crCredentials.password });
  }

  const cpeCredentials = ext("auth", "getCpeCredentials", serial);
  if (cpeCredentials && cpeCredentials.username && cpeCredentials.password) {
    declare(root + ".ManagementServer.Username", { value: now }, { value: cpeCredentials.username });
    declare(root + ".ManagementServer.Password", { value: now }, { value: cpeCredentials.password });
  }
} catch (e) {
  log("ManagementServer credential enforcement error: " + e.message);
}

// Get current management server settings
const periodicInformInterval = declare(root + ".ManagementServer.PeriodicInformInterval", { value: 1 });

// Set periodic inform interval to 5 minutes (300 seconds) if not already set
// This is idempotent - only changes if different
const targetInterval = 300;
if (periodicInformInterval.value === undefined || periodicInformInterval.value[0] !== targetInterval) {
  declare(root + ".ManagementServer.PeriodicInformInterval", { value: now }, { value: targetInterval });
}

// Enable periodic inform if disabled
const periodicInformEnable = declare(root + ".ManagementServer.PeriodicInformEnable", { value: 1 });
if (periodicInformEnable.value === undefined || periodicInformEnable.value[0] !== true) {
  declare(root + ".ManagementServer.PeriodicInformEnable", { value: now }, { value: true });
}

// Notify DotMac webhook about bootstrap event
const oui = declare("DeviceID.OUI", { value: 1 });

try {
  ext(
    "dotmac-webhook",
    "informWebhook",
    genieDeviceId,
    serial,
    "bootstrap",
    oui.value ? oui.value[0] : "",
    productClass.value ? productClass.value[0] : "",
    JSON.stringify({
      manufacturer: manufacturer.value ? manufacturer.value[0] : null,
      software_version: softwareVersion.value ? softwareVersion.value[0] : null,
      data_model: root,
    })
  );
} catch (e) {
  // Don't fail provision if webhook fails
  log("Bootstrap webhook error: " + e.message);
}

log("Bootstrap provision completed for " + (serial || "unknown"));
