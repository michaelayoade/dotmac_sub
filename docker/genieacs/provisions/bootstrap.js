/**
 * DotMac GenieACS Bootstrap Provision
 *
 * This provision runs on device bootstrap (0 BOOTSTRAP event) to:
 * 1. Clear cached data model after factory reset
 * 2. Configure periodic inform interval
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

log("Bootstrap provision completed for " + (serial || "unknown"));
