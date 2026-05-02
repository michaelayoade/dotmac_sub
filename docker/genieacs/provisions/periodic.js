/**
 * DotMac GenieACS Periodic Provision
 *
 * This provision runs on periodic inform (2 PERIODIC event) to:
 * 1. Keep periodic inform handling cheap.
 *
 * Keep this path intentionally small. Broad inventory refreshes and config
 * enforcement belong on BOOTSTRAP/BOOT, explicit tasks, or slower scheduled
 * refreshes so periodic informs stay cheap at fleet scale.
 */

// Intentionally empty. In this GenieACS build, provision scripts have a
// hard-coded 50ms VM timeout, so HTTP-backed ext() calls do not belong here.
