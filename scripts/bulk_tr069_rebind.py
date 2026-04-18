#!/usr/bin/env python3
"""Bulk TR-069 profile rebind for ACS migration.

Rebinds ONTs from their current TR-069 server profile to each OLT's linked
ACS profile, triggering a reset so ONTs register with the configured ACS.

Auto-detects the linked ACS profile per OLT by URL/username match. If the
profile doesn't exist yet, it is auto-created using the same service as the
web UI's "Init TR-069" behaviour.

Uses a single SSH session per OLT for efficiency (vs one connection per ONT).
Writes a checkpoint file per OLT so interrupted runs can be safely resumed.

Usage:
    # Dry run (default) — auto-detects profile per OLT, shows what would happen
    python scripts/bulk_tr069_rebind.py

    # Dry run on a specific OLT
    python scripts/bulk_tr069_rebind.py --olt "Garki Huawei OLT"

    # Execute (rebinds + resets ONTs)
    python scripts/bulk_tr069_rebind.py --execute

    # Execute on one OLT only
    python scripts/bulk_tr069_rebind.py --olt "Garki Huawei OLT" --execute

    # Limit batch size (e.g., first 10 per OLT for testing)
    python scripts/bulk_tr069_rebind.py --olt "Garki Huawei OLT" --limit 10 --execute

    # Resume after interrupted run (auto-skips already-rebound ONTs)
    python scripts/bulk_tr069_rebind.py --execute --resume

    # Override auto-detection with explicit profile ID
    python scripts/bulk_tr069_rebind.py --profile-id 2 --execute

    # Verify post-rebind: check GenieACS for new informs
    python scripts/bulk_tr069_rebind.py --verify
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

if TYPE_CHECKING:
    from app.models.network import OLTDevice

# Bootstrap app context
sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bulk_rebind")

# Checkpoint directory for resume capability
CHECKPOINT_DIR = Path("scripts/migration/.rebind_checkpoints")


class OntBatchItem(TypedDict):
    serial_number: str
    fsp: str
    ont_id: int
    frame_slot: str
    port_num: str
    online_status: str


class BatchStats(TypedDict):
    bound: int
    skipped: int
    errors: int
    completed_serials: set[str]


class RunTotals(TypedDict):
    bound: int
    skipped: int
    errors: int
    preflight_failed: int


# ── Checkpoint helpers ──────────────────────────────────────────────────────


def _checkpoint_path(olt_name: str, profile_id: int) -> Path:
    """Return path for this OLT's checkpoint file."""
    safe_name = re.sub(r"[^\w\-]", "_", olt_name)
    return CHECKPOINT_DIR / f"{safe_name}_profile{profile_id}.json"


def _load_checkpoint(olt_name: str, profile_id: int) -> set[str]:
    """Load set of already-rebound serial numbers for this OLT."""
    path = _checkpoint_path(olt_name, profile_id)
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        serials = set(data.get("completed", []))
        logger.info(
            "  Loaded checkpoint: %d ONTs already rebound on %s", len(serials), olt_name
        )
        return serials
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("  Corrupt checkpoint for %s, starting fresh: %s", olt_name, exc)
        return set()


def _save_checkpoint(
    olt_name: str,
    profile_id: int,
    completed: set[str],
) -> None:
    """Persist completed serial numbers to checkpoint file."""
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(olt_name, profile_id)
    data = {
        "olt_name": olt_name,
        "profile_id": profile_id,
        "completed": sorted(completed),
        "count": len(completed),
        "last_updated": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2))


# ── ONT-ID parsing ─────────────────────────────────────────────────────────


def _parse_ont_id_from_external(external_id: str | None) -> int | None:
    """Extract ONT-ID from external_id field.

    Formats:
        "5"                         → 5
        "huawei:4194320640.5"       → 5
        "smartolt:HWTC93A47984"     → None (no ONT-ID encoded)
    """
    if not external_id:
        return None
    ext = external_id.strip()
    if ext.isdigit():
        return int(ext)
    if "." in ext:
        dot_part = ext.rsplit(".", 1)[-1]
        if dot_part.isdigit():
            return int(dot_part)
    return None


def _build_fsp(board: str | None, port: str | None) -> str | None:
    """Build FSP string from board and port fields."""
    if board and port:
        return f"{board}/{port}"
    return None


# ── Pre-flight checks ──────────────────────────────────────────────────────


def _preflight_check_olt(
    olt: OLTDevice,
    profile_id: int,
) -> tuple[bool, str]:
    """Verify SSH connectivity and that the TR-069 profile exists on the OLT.

    Returns:
        (ok, message) tuple.
    """
    from app.services.network import olt_ssh as core

    try:
        transport, channel, _policy = core._open_shell(olt)
    except Exception as exc:
        return False, f"SSH connection failed: {exc}"

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)

        # Check if TR-069 profile exists
        output = core._run_huawei_cmd(
            channel,
            "display ont tr069-server-profile all",
            prompt=config_prompt,
        )

        # Look for the profile ID in the output
        profile_found = False
        for line in output.splitlines():
            # Huawei format: "  ProfileID : 2  ProfileName : GenieACS"
            match = re.search(r"ProfileID\s*:\s*(\d+)", line, re.IGNORECASE)
            if match and int(match.group(1)) == profile_id:
                profile_found = True
                break
            # Also check table format: "  2    GenieACS    ..."
            parts = line.split()
            if parts and parts[0].isdigit() and int(parts[0]) == profile_id:
                profile_found = True
                break

        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

        if not profile_found:
            return False, (
                f"TR-069 profile ID {profile_id} not found on OLT. "
                f"Available profiles:\n{output[:500]}"
            )

        return True, "SSH OK, profile exists"
    except Exception as exc:
        return False, f"Pre-flight command failed: {exc}"
    finally:
        transport.close()


# ── Profile resolution ────────────────────────────────────────────────


def _resolve_linked_acs_profile(
    olt: OLTDevice,
    *,
    auto_create: bool = False,
) -> tuple[int | None, str]:
    """Find or create the TR-069 profile for this OLT's linked ACS.

    If no match is found and ``auto_create`` is True, creates/verifies the
    profile using the shared OLT TR-069 admin service.

    Returns:
        (profile_id, message) — profile_id is None if not found/created.
    """
    from app.services.network.olt_ssh_profiles import get_tr069_server_profiles
    from app.services.network.olt_tr069_admin import (
        ensure_tr069_profile_for_linked_acs,
        linked_acs_profile_payload,
    )
    from app.services.network.tr069_profile_matching import match_tr069_profile

    payload = linked_acs_profile_payload(olt)
    if payload is None:
        return None, "No linked ACS configured on OLT"

    ok, msg, profiles = get_tr069_server_profiles(olt)
    if not ok:
        return None, f"Cannot list profiles: {msg}"

    existing = match_tr069_profile(
        profiles,
        acs_url=payload["acs_url"],
        acs_username=payload["username"],
    )
    if existing is not None:
        return (
            existing.profile_id,
            f"Found linked ACS profile '{existing.name}' (ID {existing.profile_id})",
        )

    if not auto_create:
        names = ", ".join(f"{p.name}(ID {p.profile_id})" for p in profiles) or "(none)"
        return None, f"No linked ACS profile found. Existing: {names}"

    ok, create_msg, profile_id = ensure_tr069_profile_for_linked_acs(olt)
    if not ok:
        return None, f"Auto-create failed: {create_msg}"
    if profile_id is None:
        return None, "Profile created but could not verify ID"
    return profile_id, f"Auto-created linked ACS profile (ID {profile_id})"


# ── Batch rebind ───────────────────────────────────────────────────────────


# Regex for Huawei Y/N confirmation prompts
_YN_PROMPT = re.compile(r"\[?[yY]/[nN]\]?|Are you sure|y/n", re.IGNORECASE)


def _rebind_batch_on_olt(
    olt: OLTDevice,
    onts: list[OntBatchItem],
    profile_id: int,
    *,
    dry_run: bool = True,
    delay_between: float = 1.0,
    already_done: set[str] | None = None,
) -> BatchStats:
    """Rebind a batch of ONTs on a single OLT using one SSH session.

    Args:
        olt: OLTDevice instance.
        onts: List of dicts with keys: serial_number, fsp, ont_id, frame_slot, port_num.
        profile_id: Target TR-069 profile ID on the OLT.
        dry_run: If True, log commands but don't execute.
        delay_between: Seconds to wait between ONTs (avoids overloading OLT CLI).
        already_done: Set of serial numbers to skip (from checkpoint).

    Returns:
        Stats dict: {bound, skipped, errors, completed_serials}.
    """
    from app.services.network import olt_ssh as core

    if already_done is None:
        already_done = set()

    stats: BatchStats = {
        "bound": 0,
        "skipped": 0,
        "errors": 0,
        "completed_serials": set(),
    }
    completed: set[str] = set(already_done)

    # Filter out already-done ONTs
    pending = [ont for ont in onts if ont["serial_number"] not in already_done]
    if len(pending) < len(onts):
        skipped_count = len(onts) - len(pending)
        stats["skipped"] = skipped_count
        logger.info("  Resuming: skipping %d already-rebound ONTs", skipped_count)

    if not pending:
        stats["completed_serials"] = completed
        return stats

    if dry_run:
        for ont in pending:
            logger.info(
                "  [DRY RUN] Would rebind ONT %s (ONT-ID %d on %s) → profile %d",
                ont["serial_number"],
                ont["ont_id"],
                ont["fsp"],
                profile_id,
            )
            stats["bound"] += 1
        stats["completed_serials"] = completed
        return stats

    # Open single SSH session to OLT
    try:
        transport, channel, _policy = core._open_shell(olt)
    except Exception as exc:
        logger.error("SSH connection failed to %s: %s", olt.name, exc)
        stats["errors"] = len(pending)
        stats["completed_serials"] = completed
        return stats

    start_time = time.monotonic()

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)

        current_frame_slot: str | None = None

        for i, ont in enumerate(pending, 1):
            fs = ont["frame_slot"]
            port_num = ont["port_num"]
            ont_id = ont["ont_id"]
            serial = ont["serial_number"]

            # Progress logging with ETA
            elapsed = time.monotonic() - start_time
            if i > 1:
                avg_per_ont = elapsed / (i - 1)
                remaining = avg_per_ont * (len(pending) - i + 1)
                eta_min = remaining / 60
                logger.info(
                    "  [%d/%d] ONT %s (ID %d on %s) — ETA: %.1f min",
                    i,
                    len(pending),
                    serial,
                    ont_id,
                    ont["fsp"],
                    eta_min,
                )
            else:
                logger.info(
                    "  [%d/%d] ONT %s (ID %d on %s)",
                    i,
                    len(pending),
                    serial,
                    ont_id,
                    ont["fsp"],
                )

            # Enter interface context if needed (minimize quit/re-enter)
            if fs != current_frame_slot:
                if current_frame_slot is not None:
                    core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
                core._run_huawei_cmd(
                    channel, f"interface gpon {fs}", prompt=config_prompt
                )
                current_frame_slot = fs

            # Bind TR-069 profile
            cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
            output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)

            if core.is_error_output(output):
                logger.warning(
                    "  FAILED bind for ONT %s (ID %d): %s",
                    serial,
                    ont_id,
                    output.strip()[-120:],
                )
                stats["errors"] += 1
                continue

            # Reset ONT to trigger re-registration with new ACS
            reset_out = core._run_huawei_cmd(
                channel,
                f"ont reset {port_num} {ont_id}",
                prompt=r"[#)]\s*$|\[?[yYnN/]+\]|Are you sure",
            )
            if _YN_PROMPT.search(reset_out):
                channel.send("y\n")
                core._read_until_prompt(channel, config_prompt, timeout_sec=8)

            logger.info(
                "  ✓ Rebound ONT %s (ID %d on %s) → profile %d",
                serial,
                ont_id,
                ont["fsp"],
                profile_id,
            )
            stats["bound"] += 1
            completed.add(serial)

            # Save checkpoint every 10 ONTs
            if len(completed) % 10 == 0:
                _save_checkpoint(olt.name, profile_id, completed)

            if delay_between > 0:
                time.sleep(delay_between)

        # Exit interface + config modes
        if current_frame_slot is not None:
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

    except Exception as exc:
        logger.error("SSH error on %s after %d ONTs: %s", olt.name, stats["bound"], exc)
        stats["errors"] += 1
    finally:
        transport.close()
        # Always save final checkpoint
        _save_checkpoint(olt.name, profile_id, completed)

    stats["completed_serials"] = completed
    return stats


# ── Main orchestrator ──────────────────────────────────────────────────────


def run(
    db: Session,
    *,
    profile_id: int | None = None,
    olt_name: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    resume: bool = False,
    skip_preflight: bool = False,
    auto_create_profile: bool = True,
) -> RunTotals:
    """Run the bulk rebind across OLTs.

    Args:
        db: Database session.
        profile_id: Target TR-069 OLT profile ID. If None, auto-detects
            each OLT's linked ACS profile by URL/username match.
        olt_name: If set, only rebind ONTs on this OLT.
        dry_run: If True, log commands but don't execute.
        limit: Max ONTs per OLT (for staged rollout).
        resume: If True, load checkpoint and skip already-rebound ONTs.
        skip_preflight: If True, skip SSH/profile verification.
        auto_create_profile: If True and the linked ACS profile doesn't
            exist on an OLT, create it automatically.

    Returns:
        Aggregate stats dict.
    """
    from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort

    # Build OLT filter — require SSH credentials for rebind
    olt_stmt = (
        select(OLTDevice)
        .where(
            OLTDevice.mgmt_ip.isnot(None),
            OLTDevice.ssh_username.isnot(None),
            OLTDevice.ssh_password.isnot(None),
        )
        .options(joinedload(OLTDevice.tr069_acs_server))
    )
    if olt_name:
        olt_stmt = olt_stmt.where(OLTDevice.name == olt_name)
    olts = db.scalars(olt_stmt).all()

    if not olts:
        logger.error("No OLTs found%s", f" matching '{olt_name}'" if olt_name else "")
        return {"bound": 0, "skipped": 0, "errors": 0, "preflight_failed": 0}

    totals: RunTotals = {"bound": 0, "skipped": 0, "errors": 0, "preflight_failed": 0}

    for olt in olts:
        logger.info("━━━ %s (%s) ━━━", olt.name, olt.mgmt_ip)

        # Resolve the TR-069 profile for this OLT
        if profile_id is not None:
            olt_profile_id = profile_id
            logger.info("  Using explicit profile ID %d", olt_profile_id)
        else:
            logger.info("  Auto-detecting linked ACS profile...")
            resolved_id, resolve_msg = _resolve_linked_acs_profile(
                olt,
                auto_create=auto_create_profile and not dry_run,
            )
            if resolved_id is None:
                logger.error("  SKIPPING %s — %s", olt.name, resolve_msg)
                totals["preflight_failed"] += 1
                continue
            olt_profile_id = resolved_id
            logger.info("  %s", resolve_msg)

        # Pre-flight check (skip in dry-run mode)
        if not dry_run and not skip_preflight:
            logger.info("  Running pre-flight check...")
            ok, msg = _preflight_check_olt(olt, olt_profile_id)
            if not ok:
                logger.error("  PRE-FLIGHT FAILED for %s: %s", olt.name, msg)
                totals["preflight_failed"] += 1
                continue
            logger.info("  Pre-flight OK: %s", msg)

        # Find ALL ONTs on this OLT via active assignments
        # (removed online_status filter — offline ONTs need rebind too,
        # the profile persists on OLT config and takes effect on next boot)
        stmt = (
            select(
                OntUnit.serial_number,
                OntUnit.board,
                OntUnit.port,
                OntUnit.external_id,
                OntUnit.online_status,
            )
            .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
            .join(PonPort, PonPort.id == OntAssignment.pon_port_id)
            .where(
                PonPort.olt_id == olt.id,
                OntAssignment.active.is_(True),
            )
            .order_by(OntUnit.board, OntUnit.port, OntUnit.external_id)
        )
        if limit:
            stmt = stmt.limit(limit)

        rows = db.execute(stmt).fetchall()

        # Parse ONT-IDs and filter out unparseable ones
        batch: list[OntBatchItem] = []
        online_count = 0
        offline_count = 0
        for r in rows:
            serial_number = cast(str | None, r.serial_number)
            external_id = cast(str | None, r.external_id)
            board = cast(str | None, r.board)
            port = cast(str | None, r.port)
            online_status = cast(str | None, r.online_status)

            ont_id = _parse_ont_id_from_external(external_id)
            fsp = _build_fsp(board, port)
            if ont_id is None or fsp is None:
                logger.warning(
                    "  Skipping %s — cannot resolve ONT-ID (external_id=%s, board=%s, port=%s)",
                    serial_number,
                    external_id,
                    board,
                    port,
                )
                totals["skipped"] += 1
                continue

            if online_status == "online":
                online_count += 1
            else:
                offline_count += 1

            parts = fsp.split("/")
            batch.append(
                {
                    "serial_number": serial_number or "",
                    "fsp": fsp,
                    "ont_id": ont_id,
                    "frame_slot": f"{parts[0]}/{parts[1]}",
                    "port_num": parts[2],
                    "online_status": online_status or "unknown",
                }
            )

        logger.info(
            "  %d ONTs total (%d online, %d offline), %d with valid ONT-ID, %d skipped",
            len(rows),
            online_count,
            offline_count,
            len(batch),
            len(rows) - len(batch),
        )

        if not batch:
            continue

        # Group by frame_slot for efficient SSH session usage
        batch.sort(key=lambda x: (x["frame_slot"], x["ont_id"]))

        # Load checkpoint for resume
        already_done: set[str] = set()
        if resume and not dry_run:
            already_done = _load_checkpoint(olt.name, olt_profile_id)

        stats = _rebind_batch_on_olt(
            olt,
            batch,
            olt_profile_id,
            dry_run=dry_run,
            already_done=already_done,
        )
        totals["bound"] += stats["bound"]
        totals["skipped"] += stats["skipped"]
        totals["errors"] += stats["errors"]

    return totals


# ── GenieACS verification ─────────────────────────────────────────────────


def verify_genieacs_informs(db: Session) -> dict[str, int | str]:
    """Check GenieACS for ONTs that have informed since rebind.

    Compares rebound serial numbers (from checkpoints) against
    GenieACS device list to see which have successfully migrated.
    """
    import os

    import httpx

    from app.models.network import OntUnit

    genieacs_url = os.environ.get("GENIEACS_NBI_URL", "http://localhost:7557")

    # Collect all rebound serials from checkpoint files
    rebound_serials: set[str] = set()
    if CHECKPOINT_DIR.exists():
        for cp_file in CHECKPOINT_DIR.glob("*.json"):
            try:
                data = json.loads(cp_file.read_text())
                rebound_serials.update(data.get("completed", []))
            except (json.JSONDecodeError, KeyError):
                continue

    if not rebound_serials:
        logger.warning("No checkpoint files found — run rebind first")
        return {"rebound": 0, "informed": 0, "missing": 0}

    logger.info("Found %d rebound serials from checkpoints", len(rebound_serials))

    # Query GenieACS for devices
    try:
        resp = httpx.get(
            f"{genieacs_url}/devices",
            params={"projection": "_id,_lastInform"},
            timeout=30,
        )
        resp.raise_for_status()
        genieacs_devices = resp.json()
    except Exception as exc:
        logger.error("Failed to query GenieACS: %s", exc)
        return {
            "rebound": len(rebound_serials),
            "informed": 0,
            "missing": 0,
            "error": str(exc),
        }

    # Extract serial numbers from GenieACS device IDs
    # GenieACS _id format varies: could be OUI-ProductClass-SerialNumber
    genieacs_serials: set[str] = set()
    for dev in genieacs_devices:
        device_id = dev.get("_id", "")
        # Try to extract serial from the ID
        parts = device_id.split("-")
        if len(parts) >= 3:
            genieacs_serials.add(parts[-1])
        genieacs_serials.add(device_id)

    # Cross-reference: look up DB serial numbers in GenieACS
    informed = 0
    missing_serials: list[str] = []

    for sn in sorted(rebound_serials):
        if sn in genieacs_serials or any(sn in gid for gid in genieacs_serials):
            informed += 1
        else:
            missing_serials.append(sn)

    logger.info("━━━ GenieACS Verification ━━━")
    logger.info("  Rebound:  %d ONTs", len(rebound_serials))
    logger.info(
        "  Informed: %d ONTs (%.1f%%)",
        informed,
        informed / len(rebound_serials) * 100 if rebound_serials else 0,
    )
    logger.info("  Missing:  %d ONTs", len(missing_serials))

    if missing_serials:
        # Check their online status in DB
        ont_rows = db.execute(
            select(OntUnit.serial_number, OntUnit.online_status).where(
                OntUnit.serial_number.in_(missing_serials)
            )
        ).fetchall()
        status_counts: dict[str, int] = {}
        for r in ont_rows:
            s = r.online_status or "unknown"
            status_counts[s] = status_counts.get(s, 0) + 1

        logger.info("  Missing ONTs by status: %s", status_counts)
        if len(missing_serials) <= 20:
            for sn in missing_serials:
                logger.info("    - %s", sn)

    return {
        "rebound": len(rebound_serials),
        "informed": informed,
        "missing": len(missing_serials),
    }


# ── CLI entry point ────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk TR-069 profile rebind for SmartOLT → GenieACS migration",
    )
    parser.add_argument(
        "--profile-id",
        type=int,
        default=None,
        help="Override auto-detection with explicit OLT profile ID",
    )
    parser.add_argument(
        "--olt",
        type=str,
        default=None,
        help="Only rebind ONTs on this OLT (exact name match)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute the rebind (default is dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max ONTs per OLT (for staged rollout)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from checkpoint (skip already-rebound ONTs)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip SSH/profile pre-flight check",
    )
    parser.add_argument(
        "--no-auto-create",
        action="store_true",
        help="Don't auto-create the linked ACS profile on OLTs that lack it",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify GenieACS has received informs from rebound ONTs",
    )
    args = parser.parse_args()

    from app.db import SessionLocal

    db = SessionLocal()

    try:
        if args.verify:
            verify_genieacs_informs(db)
            return

        dry_run = not args.execute
        mode = "DRY RUN" if dry_run else "EXECUTE"
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║  Bulk TR-069 Rebind [%s]           ║", mode.ljust(7))
        logger.info("╚══════════════════════════════════════════╝")
        if args.profile_id:
            logger.info("Profile ID: %d (explicit override)", args.profile_id)
        else:
            logger.info("Profile: auto-detect per OLT (linked ACS by URL/username)")
            if not args.no_auto_create:
                logger.info(
                    "Auto-create: ON (will create profile on OLTs that lack it)"
                )
        if args.olt:
            logger.info("OLT filter: %s", args.olt)
        if args.limit:
            logger.info("Limit: %d per OLT", args.limit)
        if args.resume:
            logger.info("Resume mode: ON")
        logger.info("")

        totals = run(
            db,
            profile_id=args.profile_id,
            olt_name=args.olt,
            dry_run=dry_run,
            limit=args.limit,
            resume=args.resume,
            skip_preflight=args.skip_preflight,
            auto_create_profile=not args.no_auto_create,
        )
        logger.info("")
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║  Summary                                ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info("║  Bound:            %s║", str(totals["bound"]).ljust(22))
        logger.info("║  Skipped:          %s║", str(totals["skipped"]).ljust(22))
        logger.info("║  Errors:           %s║", str(totals["errors"]).ljust(22))
        if totals.get("preflight_failed"):
            logger.info(
                "║  Preflight failed: %s║", str(totals["preflight_failed"]).ljust(22)
            )
        logger.info("╚══════════════════════════════════════════╝")

        if dry_run:
            logger.info("")
            logger.info("This was a DRY RUN. Add --execute to actually rebind.")
            logger.info(
                "Tip: Use --execute --resume to safely resume after interruption."
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
