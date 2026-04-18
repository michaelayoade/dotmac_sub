"""OLT CLI output capture for building parser test fixtures.

This module captures real CLI output from OLTs and stores it for:
1. Building test fixtures for TextFSM templates
2. Detecting parsing regressions when firmware changes
3. Debugging parsing issues with real data

Usage:
    from app.services.network.olt_cli_capture import capture_olt_samples

    # Capture samples from an OLT (e.g., after adding a new OLT)
    result = capture_olt_samples(db, olt_id)

    # Or via Celery task
    from app.tasks.olt_capture import capture_olt_samples_task
    from app.celery_app import enqueue_celery_task
    enqueue_celery_task(capture_olt_samples_task, args=[str(olt_id)])
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app.models.network import OLTDevice

logger = logging.getLogger(__name__)

# Directory for storing captured CLI samples
SAMPLES_DIR = Path(__file__).parent.parent.parent.parent / "data" / "olt_samples"

# Commands to capture for each vendor
CAPTURE_COMMANDS: dict[str, list[tuple[str, str]]] = {
    "huawei": [
        ("display_version", "display version"),
        ("display_ont_autofind", "display ont autofind all"),
        ("display_ont_info_0_0", "display ont info 0 0 all"),
        ("display_ont_info_0_1", "display ont info 0 1 all"),
        ("display_ont_info_0_2", "display ont info 0 2 all"),
        ("display_service_port_0_0_0", "display service-port port 0/0/0"),
        ("display_service_port_0_1_0", "display service-port port 0/1/0"),
        ("display_service_port_0_2_0", "display service-port port 0/2/0"),
        ("display_ont_lineprofile", "display ont-lineprofile gpon all"),
        ("display_ont_srvprofile", "display ont-srvprofile gpon all"),
        ("display_tr069_profile_all", "display ont tr069-server-profile all"),
    ],
}


@dataclass
class CaptureMetadata:
    """Metadata about a CLI capture session."""

    olt_id: str
    olt_name: str
    vendor: str
    model: str
    firmware_version: str | None
    captured_at: str
    capture_duration_sec: float
    commands_captured: int
    commands_failed: int
    errors: list[str] = field(default_factory=list)


@dataclass
class CommandCapture:
    """A single captured CLI command output."""

    command_key: str
    command: str
    output: str
    success: bool
    error: str | None = None
    duration_sec: float = 0.0


def _get_olt_sample_dir(olt: OLTDevice) -> Path:
    """Get the directory for storing samples from an OLT."""
    vendor = (olt.vendor or "unknown").lower().replace(" ", "_")
    model = (olt.model or "unknown").lower().replace(" ", "_")
    olt_name = (olt.name or str(olt.id)).lower().replace(" ", "_")
    return SAMPLES_DIR / vendor / model / olt_name


def _extract_firmware_version(version_output: str, vendor: str) -> str | None:
    """Extract firmware version from display version output."""
    import re

    if "huawei" in vendor.lower():
        # Look for patterns like "V800R021C00SPC100" or "Version 8.210"
        match = re.search(r"(V\d+R\d+C\d+\w*)", version_output)
        if match:
            return match.group(1)
        match = re.search(r"Version\s+([\d.]+)", version_output)
        if match:
            return match.group(1)
    return None


def capture_olt_samples(
    db: Session,
    olt_id: UUID,
    *,
    force: bool = False,
    commands: list[str] | None = None,
) -> tuple[bool, str, CaptureMetadata | None]:
    """Capture CLI output samples from an OLT for parser testing.

    Args:
        db: Database session.
        olt_id: UUID of the OLT to capture from.
        force: If True, capture even if recent samples exist.
        commands: Optional list of command keys to capture (default: all).

    Returns:
        Tuple of (success, message, metadata).
    """
    import time

    from sqlalchemy import select

    from app.services.network import olt_ssh

    # Load OLT
    stmt = select(OLTDevice).where(OLTDevice.id == olt_id)
    olt = db.scalars(stmt).first()
    if not olt:
        return False, f"OLT {olt_id} not found", None

    vendor = (olt.vendor or "").lower()
    if vendor not in CAPTURE_COMMANDS:
        return False, f"No capture commands defined for vendor: {olt.vendor}", None

    # Check if recent samples exist
    sample_dir = _get_olt_sample_dir(olt)
    metadata_file = sample_dir / "metadata.json"
    if not force and metadata_file.exists():
        try:
            existing = json.loads(metadata_file.read_text())
            captured_at = datetime.fromisoformat(existing["captured_at"])
            age_hours = (datetime.now(UTC) - captured_at).total_seconds() / 3600
            if age_hours < 24:
                return (
                    True,
                    f"Recent samples exist (captured {age_hours:.1f}h ago)",
                    None,
                )
        except (json.JSONDecodeError, KeyError):
            pass

    # Ensure directory exists
    sample_dir.mkdir(parents=True, exist_ok=True)

    # Determine which commands to capture
    all_commands = CAPTURE_COMMANDS[vendor]
    if commands:
        all_commands = [(k, c) for k, c in all_commands if k in commands]

    start_time = time.monotonic()
    captures: list[CommandCapture] = []
    errors: list[str] = []
    firmware_version: str | None = None

    # Open SSH connection
    try:
        transport, channel, policy = olt_ssh._open_shell(olt)
    except Exception as e:
        logger.error("Failed to connect to OLT %s: %s", olt.name, e)
        return False, f"SSH connection failed: {e}", None

    try:
        # Enter enable mode
        channel.send("enable\n")
        olt_ssh._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        channel.send("screen-length 0 temporary\n")
        olt_ssh._read_until_prompt(channel, r"#\s*$", timeout_sec=5)

        for cmd_key, cmd in all_commands:
            cmd_start = time.monotonic()
            try:
                output = olt_ssh._run_huawei_cmd(channel, cmd, prompt=r"#\s*$")
                duration = time.monotonic() - cmd_start

                captures.append(
                    CommandCapture(
                        command_key=cmd_key,
                        command=cmd,
                        output=output,
                        success=True,
                        duration_sec=duration,
                    )
                )

                # Extract firmware from version command
                if cmd_key == "display_version" and not firmware_version:
                    firmware_version = _extract_firmware_version(output, vendor)

                logger.debug(
                    "Captured %s from OLT %s (%.1fs)", cmd_key, olt.name, duration
                )

            except Exception as e:
                duration = time.monotonic() - cmd_start
                error_msg = f"{cmd_key}: {e}"
                errors.append(error_msg)
                captures.append(
                    CommandCapture(
                        command_key=cmd_key,
                        command=cmd,
                        output="",
                        success=False,
                        error=str(e),
                        duration_sec=duration,
                    )
                )
                logger.warning(
                    "Failed to capture %s from OLT %s: %s", cmd_key, olt.name, e
                )

    finally:
        transport.close()

    total_duration = time.monotonic() - start_time

    # Save captures to files
    for capture in captures:
        if capture.success and capture.output.strip():
            output_file = sample_dir / f"{capture.command_key}.txt"
            output_file.write_text(capture.output)

    # Build metadata
    metadata = CaptureMetadata(
        olt_id=str(olt.id),
        olt_name=olt.name or "",
        vendor=olt.vendor or "",
        model=olt.model or "",
        firmware_version=firmware_version,
        captured_at=datetime.now(UTC).isoformat(),
        capture_duration_sec=round(total_duration, 2),
        commands_captured=sum(1 for c in captures if c.success),
        commands_failed=sum(1 for c in captures if not c.success),
        errors=errors,
    )

    # Save metadata
    metadata_file.write_text(json.dumps(asdict(metadata), indent=2))

    # Save individual command metadata
    commands_file = sample_dir / "commands.json"
    commands_data = [asdict(c) for c in captures]
    commands_file.write_text(json.dumps(commands_data, indent=2))

    msg = (
        f"Captured {metadata.commands_captured} commands from {olt.name} "
        f"({metadata.commands_failed} failed) in {total_duration:.1f}s"
    )
    logger.info(msg)

    return True, msg, metadata


def validate_parsers_against_samples(
    olt_id: UUID | None = None,
    vendor: str | None = None,
) -> dict[str, list[dict]]:
    """Validate TextFSM parsers against captured samples.

    Args:
        olt_id: Optional specific OLT to validate.
        vendor: Optional vendor filter.

    Returns:
        Dict of {command_key: [validation results]}.
    """
    from app.services.network.parsers import (
        parse_autofind,
        parse_ont_info,
        parse_profile_table,
        parse_service_port_table,
    )

    # Map command keys to parser functions
    parsers: dict[str, Callable[[str, str], Any]] = {
        "display_ont_autofind": parse_autofind,
        "display_ont_info_0_0": parse_ont_info,
        "display_ont_info_0_1": parse_ont_info,
        "display_ont_info_0_2": parse_ont_info,
        "display_service_port_0_0_0": parse_service_port_table,
        "display_service_port_0_1_0": parse_service_port_table,
        "display_service_port_0_2_0": parse_service_port_table,
        "display_ont_lineprofile": parse_profile_table,
        "display_ont_srvprofile": parse_profile_table,
        "display_tr069_profile_all": parse_profile_table,
    }

    results: dict[str, list[dict]] = {}

    if not SAMPLES_DIR.exists():
        return results

    # Find sample directories
    for vendor_dir in SAMPLES_DIR.iterdir():
        if not vendor_dir.is_dir():
            continue
        if vendor and vendor_dir.name != vendor.lower():
            continue

        for model_dir in vendor_dir.iterdir():
            if not model_dir.is_dir():
                continue

            for olt_dir in model_dir.iterdir():
                if not olt_dir.is_dir():
                    continue

                metadata_file = olt_dir / "metadata.json"
                if not metadata_file.exists():
                    continue

                try:
                    metadata = json.loads(metadata_file.read_text())
                except json.JSONDecodeError:
                    continue

                if olt_id and metadata.get("olt_id") != str(olt_id):
                    continue

                # Validate each captured command
                for cmd_key, parser_fn in parsers.items():
                    sample_file = olt_dir / f"{cmd_key}.txt"
                    if not sample_file.exists():
                        continue

                    output = sample_file.read_text()
                    if not output.strip():
                        continue

                    try:
                        result = parser_fn(
                            output,
                            str(metadata.get("vendor") or vendor_dir.name or "huawei"),
                        )
                        validation = {
                            "olt_id": metadata.get("olt_id"),
                            "olt_name": metadata.get("olt_name"),
                            "vendor": metadata.get("vendor"),
                            "model": metadata.get("model"),
                            "firmware": metadata.get("firmware_version"),
                            "success": result.success,
                            "row_count": result.row_count,
                            "confidence": result.confidence,
                            "warnings": result.warnings,
                            "sample_file": str(sample_file),
                        }
                    except Exception as e:
                        validation = {
                            "olt_id": metadata.get("olt_id"),
                            "olt_name": metadata.get("olt_name"),
                            "vendor": metadata.get("vendor"),
                            "model": metadata.get("model"),
                            "firmware": metadata.get("firmware_version"),
                            "success": False,
                            "error": str(e),
                            "sample_file": str(sample_file),
                        }

                    if cmd_key not in results:
                        results[cmd_key] = []
                    results[cmd_key].append(validation)

    return results


def list_captured_olts() -> list[dict[str, Any]]:
    """List all OLTs with captured samples."""
    olts: list[dict[str, Any]] = []

    if not SAMPLES_DIR.exists():
        return olts

    for vendor_dir in SAMPLES_DIR.iterdir():
        if not vendor_dir.is_dir():
            continue

        for model_dir in vendor_dir.iterdir():
            if not model_dir.is_dir():
                continue

            for olt_dir in model_dir.iterdir():
                if not olt_dir.is_dir():
                    continue

                metadata_file = olt_dir / "metadata.json"
                if metadata_file.exists():
                    try:
                        metadata = json.loads(metadata_file.read_text())
                        metadata["sample_dir"] = str(olt_dir)
                        olts.append(metadata)
                    except json.JSONDecodeError:
                        pass

    return olts
