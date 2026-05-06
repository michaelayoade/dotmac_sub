#!/usr/bin/env python3
"""Create WAN profiles on OLTs that are missing them.

This script connects to each OLT via SSH and creates a WAN profile
with NAT enabled, which is required for OMCI-first provisioning.

Usage:
    python scripts/create_wan_profiles.py [--dry-run] --profile-id N [--olt NAME]

Options:
    --dry-run       Show what would be done without making changes
    --profile-id N  WAN profile ID to create (required; choose from the OLT plan)
    --olt NAME      Only process the specified OLT (can be repeated)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models.network import OLTDevice
import time

from app.services.network.olt_ssh import (
    _open_shell,
    _read_until_prompt,
    _run_huawei_cmd,
    _SSH_CONNECTION_ERRORS,
)
from app.services.network.olt_ssh_ont._common import _send_slow


def _send_char_by_char(channel, command: str, char_delay: float = 0.02) -> None:
    """Send command character by character with delays.

    Some OLT terminals strip spaces when sent in bursts.
    This sends each character individually with a small delay.
    """
    for char in command:
        channel.send(char)
        time.sleep(char_delay)
    time.sleep(0.1)
    channel.send("\n")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _send_with_confirm(channel, command: str, timeout: int = 15) -> str:
    """Send command that may require Enter confirmation (Huawei optional args)."""
    channel.send(f"{command}\n")
    output = _read_until_prompt(channel, r"[#)]\s*$|<cr>|\|<K>", timeout_sec=timeout)

    # If we hit an optional argument prompt, send Enter to continue
    if "<cr>" in output or "|<K>" in output:
        channel.send("\n")
        output += _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=timeout)

    return output


def check_wan_profile_supported(channel) -> tuple[bool, str]:
    """Check if the OLT firmware supports ont wan-profile command.

    Uses display command to test support rather than relying on
    potentially truncated help output.

    Returns:
        (supported, output) tuple
    """
    output = _send_with_confirm(channel, "display ont wan-profile all")

    # "Unknown command" or "Unrecognized command" means not supported
    if "unknown command" in output.lower() or "unrecognized" in output.lower():
        return False, output

    # If we get here, command is supported (even if no profiles exist)
    return True, output


def check_wan_profile_exists(channel, profile_id: int, display_output: str = "") -> tuple[bool, str | None]:
    """Check if a WAN profile already exists on the OLT.

    Args:
        channel: SSH channel
        profile_id: Profile ID to check
        display_output: Optional cached output from display ont wan-profile all

    Returns:
        (exists, existing_name) tuple - existing_name is the profile name if it exists
    """
    import re

    # Use cached display output if provided
    if not display_output:
        display_output = _send_with_confirm(channel, "display ont wan-profile all")

    # Look for the profile ID in the table format:
    # Profile-ID  Profile-name                                Binding times
    # 10          wan-profile_10                              0
    pattern = rf'^\s*{profile_id}\s+(\S+)\s+\d+'
    for line in display_output.split('\n'):
        match = re.match(pattern, line.strip())
        if match:
            return True, match.group(1)

    return False, None


def create_wan_profile(
    olt: OLTDevice,
    profile_id: int,
    profile_name: str,
    *,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Create a WAN profile on an OLT.

    Args:
        olt: OLT device to configure
        profile_id: WAN profile ID (1-65535, typically 10)
        profile_name: Human-readable profile name
        dry_run: If True, don't make changes

    Returns:
        (success, message) tuple
    """
    if dry_run:
        return True, f"[DRY RUN] Would create wan-profile {profile_id} '{profile_name}'"

    try:
        transport, channel, policy = _open_shell(olt)
    except (*_SSH_CONNECTION_ERRORS, ValueError) as exc:
        return False, f"SSH connection failed: {exc}"

    try:
        # Enter enable mode
        channel.send("enable\n")
        _read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        # Enter config mode first to check command availability
        channel.send("config\n")
        _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=8)

        # Check if firmware supports wan-profile command (also gets all profiles)
        supported, display_output = check_wan_profile_supported(channel)
        if not supported:
            return False, "Firmware does not support ont wan-profile (MA5608T)"

        # Check if profile already exists (use cached display output)
        exists, existing_name = check_wan_profile_exists(channel, profile_id, display_output)
        if exists:
            return True, f"WAN profile {profile_id} already exists (name: {existing_name})"

        # Create WAN profile using char-by-char send to avoid MA5608T terminal corruption
        # ont wan-profile profile-id 10 profile-name "dotmac-wan"
        cmd = f'ont wan-profile profile-id {profile_id} profile-name "{profile_name}"'
        _send_char_by_char(channel, cmd)
        output = _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=10)

        if "failure" in output.lower() or "unknown command" in output.lower():
            return False, f"Failed to create profile: {output[-200:]}"

        # Enable NAT
        _send_char_by_char(channel, "nat enable")
        output = _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=8)
        if "failure" in output.lower() or "error" in output.lower():
            return False, f"Failed to enable NAT: {output[-200:]}"

        # Commit and exit profile config
        _send_char_by_char(channel, "commit")
        _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=8)
        _send_char_by_char(channel, "quit")
        _read_until_prompt(channel, r"[#)]\s*$", timeout_sec=8)

        # Exit config mode
        _send_char_by_char(channel, "quit")
        _read_until_prompt(channel, r"[#>]\s*$", timeout_sec=8)

        # Save configuration
        channel.send("save\n")
        # Handle confirmation prompt
        save_output = _read_until_prompt(
            channel, r"[#>]\s*$|[Yy]/[Nn]|[Cc]onfirm", timeout_sec=10
        )
        if "y/n" in save_output.lower() or "confirm" in save_output.lower():
            channel.send("y\n")
            _read_until_prompt(channel, r"[#>]\s*$", timeout_sec=30)

        return True, f"Created WAN profile {profile_id} with NAT enabled"

    except (*_SSH_CONNECTION_ERRORS, RuntimeError) as exc:
        return False, f"Error during configuration: {exc}"
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(
        description="Create WAN profiles on OLTs for OMCI-first provisioning"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--profile-id",
        type=int,
        required=True,
        help="WAN profile ID to create",
    )
    parser.add_argument(
        "--profile-name",
        default="dotmac-wan",
        help="WAN profile name (default: dotmac-wan)",
    )
    parser.add_argument(
        "--olt",
        action="append",
        dest="olts",
        metavar="NAME",
        help="Only process specified OLT(s) (can be repeated)",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        stmt = select(OLTDevice).order_by(OLTDevice.name)
        olts = db.scalars(stmt).all()

        if args.olts:
            # Filter to specified OLTs
            olt_names_lower = [n.lower() for n in args.olts]
            olts = [o for o in olts if (o.name or "").lower() in olt_names_lower]
            if not olts:
                logger.error("No matching OLTs found for: %s", args.olts)
                return 1

        logger.info("=" * 60)
        logger.info("WAN Profile Creation Script")
        logger.info("Profile ID: %d, Name: %s", args.profile_id, args.profile_name)
        if args.dry_run:
            logger.info("DRY RUN MODE - No changes will be made")
        logger.info("=" * 60)

        results = {"success": [], "failed": [], "skipped": []}

        for olt in olts:
            logger.info("")
            logger.info("Processing: %s", olt.name)

            # Check if OLT has SSH credentials
            if not olt.ssh_username or not olt.ssh_password:
                logger.warning("  Skipped: Missing SSH credentials")
                results["skipped"].append((olt.name, "Missing SSH credentials"))
                continue

            if not olt.mgmt_ip and not olt.hostname:
                logger.warning("  Skipped: No management IP or hostname")
                results["skipped"].append((olt.name, "No management IP"))
                continue

            success, message = create_wan_profile(
                olt,
                args.profile_id,
                args.profile_name,
                dry_run=args.dry_run,
            )

            if success:
                logger.info("  ✓ %s", message)
                results["success"].append((olt.name, message))
            else:
                logger.error("  ✗ %s", message)
                results["failed"].append((olt.name, message))

        # Summary
        logger.info("")
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info("Success: %d", len(results["success"]))
        for name, msg in results["success"]:
            logger.info("  ✓ %s: %s", name, msg)

        if results["skipped"]:
            logger.info("Skipped: %d", len(results["skipped"]))
            for name, msg in results["skipped"]:
                logger.info("  - %s: %s", name, msg)

        if results["failed"]:
            logger.info("Failed: %d", len(results["failed"]))
            for name, msg in results["failed"]:
                logger.error("  ✗ %s: %s", name, msg)

        if not args.dry_run and results["success"]:
            logger.info("")
            logger.info("NEXT STEP: Update DotMac UI")
            logger.info("Set 'WAN Profile' = %d for each OLT in the admin interface", args.profile_id)

        return 1 if results["failed"] else 0

    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
