#!/usr/bin/env python3
"""GenieACS Setup Script.

Deploys provisions, virtual parameters, presets, and configuration to GenieACS.
Run this after deploying GenieACS to configure it for DotMac integration.

Usage:
    python scripts/setup_genieacs.py [--base-url URL] [--dry-run]

Options:
    --base-url URL    GenieACS NBI URL (default: http://localhost:7557)
    --dry-run         Print what would be done without making changes
    --provisions      Deploy only provisions
    --virtual-params  Deploy only virtual parameters
    --presets         Deploy only presets
    --config          Deploy only config entries
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Paths to GenieACS scripts
PROVISIONS_DIR = PROJECT_ROOT / "docker" / "genieacs" / "provisions"
VIRTUAL_PARAMS_DIR = PROJECT_ROOT / "docker" / "genieacs" / "virtual-parameters"

# Preset definitions: map provision names to event triggers
PRESET_DEFINITIONS = {
    "dotmac-bootstrap": {
        "provision": "bootstrap",
        "events": {"0 BOOTSTRAP": True},
        "weight": 0,
        "precondition": "",  # Matches all devices
    },
    "dotmac-periodic": {
        "provision": "periodic",
        "events": {"2 PERIODIC": True},
        "weight": 0,
        "precondition": "",
    },
}

# Config entries to set in GenieACS
CONFIG_ENTRIES = {
    # CPE authentication using auth extension
    "cwmp.auth": 'EXT("auth", "authenticateCpe", username, password, DeviceID.ID, DeviceID.SerialNumber)',
    # Connection-request authentication using per-device/effective config.
    "cwmp.connectionRequestAuth": 'EXT("auth", "connectionRequest", DeviceID.ID, DeviceID.SerialNumber)',
}

# Legacy ad-hoc provisions that are too heavy for GenieACS's 50ms provision VM
# budget, or have been replaced by the managed bootstrap/periodic provisions.
LEGACY_PRESET_IDS = {
    "dotmac-runtime-collect",
    "dotmac-inform-webhook",
}
LEGACY_PROVISION_IDS = {
    "dotmac-runtime-collect",
    "dotmac-inform-webhook",
    "full-refresh",
}


class GenieACSSetup:
    """Handles deployment of GenieACS configuration."""

    def __init__(self, base_url: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0)

    def close(self):
        self.client.close()

    def _request(
        self,
        method: str,
        path: str,
        content: str | None = None,
        json_data: dict | None = None,
    ) -> httpx.Response | None:
        """Make HTTP request to GenieACS NBI."""
        if self.dry_run:
            logger.info("[DRY-RUN] %s %s", method, path)
            return None

        try:
            if content is not None:
                response = self.client.request(method, path, content=content)
            elif json_data is not None:
                response = self.client.request(method, path, json=json_data)
            else:
                response = self.client.request(method, path)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error %d: %s", e.response.status_code, e.response.text)
            raise
        except httpx.RequestError as e:
            logger.error("Request error: %s", e)
            raise

    def deploy_provisions(self) -> dict[str, str]:
        """Deploy provision scripts to GenieACS."""
        logger.info("Deploying provisions from %s", PROVISIONS_DIR)
        results = {}

        if not PROVISIONS_DIR.exists():
            logger.warning("Provisions directory not found: %s", PROVISIONS_DIR)
            return results

        for script_path in PROVISIONS_DIR.glob("*.js"):
            provision_name = script_path.stem
            script_content = script_path.read_text()

            logger.info("Deploying provision: %s", provision_name)
            try:
                self._request("PUT", f"/provisions/{provision_name}", content=script_content)
                results[provision_name] = "deployed"
                logger.info("  ✓ Deployed %s", provision_name)
            except Exception as e:
                results[provision_name] = f"error: {e}"
                logger.error("  ✗ Failed to deploy %s: %s", provision_name, e)

        return results

    def deploy_virtual_parameters(self) -> dict[str, str]:
        """Deploy virtual parameter scripts to GenieACS."""
        logger.info("Deploying virtual parameters from %s", VIRTUAL_PARAMS_DIR)
        results = {}

        if not VIRTUAL_PARAMS_DIR.exists():
            logger.warning("Virtual parameters directory not found: %s", VIRTUAL_PARAMS_DIR)
            return results

        for script_path in VIRTUAL_PARAMS_DIR.glob("*.js"):
            param_name = script_path.stem
            script_content = script_path.read_text()

            logger.info("Deploying virtual parameter: %s", param_name)
            try:
                self._request(
                    "PUT", f"/virtualParameters/{param_name}", content=script_content
                )
                results[param_name] = "deployed"
                logger.info("  ✓ Deployed %s", param_name)
            except Exception as e:
                results[param_name] = f"error: {e}"
                logger.error("  ✗ Failed to deploy %s: %s", param_name, e)

        return results

    def deploy_presets(self) -> dict[str, str]:
        """Deploy preset configurations to GenieACS."""
        logger.info("Deploying presets")
        results = {}

        for preset_name, config in PRESET_DEFINITIONS.items():
            preset_data = {
                "_id": preset_name,
                "weight": config["weight"],
                "precondition": config["precondition"],
                "events": config["events"],
                "configurations": [
                    {
                        "type": "provision",
                        "name": config["provision"],
                        "args": [],
                    }
                ],
            }

            logger.info("Deploying preset: %s (triggers: %s)", preset_name, config["provision"])
            try:
                self._request("PUT", f"/presets/{preset_name}", json_data=preset_data)
                results[preset_name] = "deployed"
                logger.info("  ✓ Deployed %s", preset_name)
            except Exception as e:
                results[preset_name] = f"error: {e}"
                logger.error("  ✗ Failed to deploy %s: %s", preset_name, e)

        return results

    def deploy_config(self) -> dict[str, str]:
        """Deploy config entries to GenieACS."""
        logger.info("Deploying config entries")
        results = {}

        for key, value in CONFIG_ENTRIES.items():
            config_data = {"_id": key, "value": value}

            logger.info("Deploying config: %s", key)
            try:
                self._request("PUT", f"/config/{key}", json_data=config_data)
                results[key] = "deployed"
                logger.info("  ✓ Deployed %s", key)
            except Exception as e:
                results[key] = f"error: {e}"
                logger.error("  ✗ Failed to deploy %s: %s", key, e)

        return results

    def prune_legacy_objects(self) -> dict[str, str]:
        """Remove known stale GenieACS objects from older deployments."""
        logger.info("Pruning legacy GenieACS objects")
        results = {}

        for preset_id in sorted(LEGACY_PRESET_IDS):
            key = f"preset:{preset_id}"
            try:
                self._request("DELETE", f"/presets/{preset_id}")
                results[key] = "deleted"
                logger.info("  ✓ Deleted legacy preset %s", preset_id)
            except Exception as e:
                results[key] = f"error: {e}"
                logger.error("  ✗ Failed to delete legacy preset %s: %s", preset_id, e)

        for provision_id in sorted(LEGACY_PROVISION_IDS):
            key = f"provision:{provision_id}"
            try:
                self._request("DELETE", f"/provisions/{provision_id}")
                results[key] = "deleted"
                logger.info("  ✓ Deleted legacy provision %s", provision_id)
            except Exception as e:
                results[key] = f"error: {e}"
                logger.error(
                    "  ✗ Failed to delete legacy provision %s: %s",
                    provision_id,
                    e,
                )

        return results

    def verify_connection(self) -> bool:
        """Verify connection to GenieACS NBI."""
        logger.info("Verifying connection to GenieACS at %s", self.base_url)
        try:
            response = self.client.get("/provisions/")
            response.raise_for_status()
            logger.info("  ✓ Connected to GenieACS NBI")
            return True
        except Exception as e:
            logger.error("  ✗ Cannot connect to GenieACS: %s", e)
            return False

    def list_current_state(self) -> dict[str, list[str]]:
        """List current provisions, virtual parameters, and presets."""
        state = {"provisions": [], "virtualParameters": [], "presets": [], "config": []}

        try:
            response = self.client.get("/provisions/")
            if response.status_code == 200:
                for item in response.json():
                    state["provisions"].append(item.get("_id", "unknown"))
        except Exception:
            pass

        try:
            response = self.client.get("/virtualParameters/")
            if response.status_code == 200:
                for item in response.json():
                    state["virtualParameters"].append(item.get("_id", "unknown"))
        except Exception:
            pass

        try:
            response = self.client.get("/presets/")
            if response.status_code == 200:
                for item in response.json():
                    state["presets"].append(item.get("_id", "unknown"))
        except Exception:
            pass

        try:
            response = self.client.get("/config/")
            if response.status_code == 200:
                for item in response.json():
                    state["config"].append(item.get("_id", "unknown"))
        except Exception:
            pass

        return state

    def run_full_setup(self) -> dict[str, dict[str, str]]:
        """Run full GenieACS setup."""
        results = {
            "legacyPrune": self.prune_legacy_objects(),
            "provisions": self.deploy_provisions(),
            "virtualParameters": self.deploy_virtual_parameters(),
            "presets": self.deploy_presets(),
            "config": self.deploy_config(),
        }
        return results


def main():
    parser = argparse.ArgumentParser(
        description="Deploy GenieACS provisions, virtual parameters, presets, and config."
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("GENIEACS_NBI_URL", "http://localhost:7557"),
        help="GenieACS NBI URL (default: http://localhost:7557)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without making changes",
    )
    parser.add_argument(
        "--provisions",
        action="store_true",
        help="Deploy only provisions",
    )
    parser.add_argument(
        "--virtual-params",
        action="store_true",
        help="Deploy only virtual parameters",
    )
    parser.add_argument(
        "--presets",
        action="store_true",
        help="Deploy only presets",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Deploy only config entries",
    )
    parser.add_argument(
        "--prune-legacy",
        action="store_true",
        help="Remove known legacy GenieACS presets/provisions",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List current GenieACS state",
    )

    args = parser.parse_args()

    setup = GenieACSSetup(args.base_url, dry_run=args.dry_run)

    try:
        if not args.dry_run and not setup.verify_connection():
            logger.error("Cannot proceed without GenieACS connection")
            sys.exit(1)

        if args.list:
            state = setup.list_current_state()
            print("\nCurrent GenieACS State:")
            print(f"  Provisions: {', '.join(state['provisions']) or 'none'}")
            print(f"  Virtual Parameters: {', '.join(state['virtualParameters']) or 'none'}")
            print(f"  Presets: {', '.join(state['presets']) or 'none'}")
            print(f"  Config: {', '.join(state['config']) or 'none'}")
            return

        # If specific flags are set, only run those
        specific_run = (
            args.provisions
            or args.virtual_params
            or args.presets
            or args.config
            or args.prune_legacy
        )

        results = {}
        if args.prune_legacy or not specific_run:
            results["legacyPrune"] = setup.prune_legacy_objects()
        if args.provisions or not specific_run:
            results["provisions"] = setup.deploy_provisions()
        if args.virtual_params or not specific_run:
            results["virtualParameters"] = setup.deploy_virtual_parameters()
        if args.presets or not specific_run:
            results["presets"] = setup.deploy_presets()
        if args.config or not specific_run:
            results["config"] = setup.deploy_config()

        # Summary
        print("\n" + "=" * 60)
        print("GenieACS Setup Summary")
        print("=" * 60)

        total_deployed = 0
        total_errors = 0

        for category, items in results.items():
            deployed = sum(1 for v in items.values() if v in {"deployed", "deleted"})
            errors = sum(1 for v in items.values() if v.startswith("error"))
            total_deployed += deployed
            total_errors += errors
            print(f"\n{category}:")
            for name, status in items.items():
                symbol = "✓" if status in {"deployed", "deleted"} else "✗"
                print(f"  {symbol} {name}: {status}")

        print("\n" + "-" * 60)
        print(f"Total: {total_deployed} deployed, {total_errors} errors")

        if total_errors > 0:
            sys.exit(1)

    finally:
        setup.close()


if __name__ == "__main__":
    main()
