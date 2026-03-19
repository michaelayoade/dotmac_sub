#!/usr/bin/env python3
"""Bulk TR-069 profile rebind for SmartOLT → GenieACS migration.

Rebinds ONTs from their current TR-069 server profile to a new profile ID
on the OLT, triggering a reset so ONTs register with the new ACS.

Uses a single SSH session per OLT for efficiency (vs one connection per ONT).

Usage:
    # Dry run (default) — shows what would happen
    python scripts/bulk_tr069_rebind.py --profile-id 2

    # Dry run on a specific OLT
    python scripts/bulk_tr069_rebind.py --profile-id 2 --olt "Garki Huawei OLT"

    # Execute (rebinds + resets ONTs)
    python scripts/bulk_tr069_rebind.py --profile-id 2 --execute

    # Execute on one OLT only
    python scripts/bulk_tr069_rebind.py --profile-id 2 --olt "Garki Huawei OLT" --execute

    # Limit batch size (e.g., first 10 per OLT for testing)
    python scripts/bulk_tr069_rebind.py --profile-id 2 --olt "Garki Huawei OLT" --limit 10 --execute
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

# Bootstrap app context
sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bulk_rebind")


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


def _rebind_batch_on_olt(
    olt: object,
    onts: list[dict],
    profile_id: int,
    *,
    dry_run: bool = True,
    delay_between: float = 1.0,
) -> dict[str, int]:
    """Rebind a batch of ONTs on a single OLT using one SSH session.

    Args:
        olt: OLTDevice instance.
        onts: List of dicts with keys: serial_number, fsp, ont_id, frame_slot, port_num.
        profile_id: Target TR-069 profile ID on the OLT.
        dry_run: If True, log commands but don't execute.
        delay_between: Seconds to wait between ONTs (avoids overloading OLT CLI).

    Returns:
        Stats dict: {bound, skipped, errors}.
    """
    from app.services.network import olt_ssh as core

    stats = {"bound": 0, "skipped": 0, "errors": 0}

    if dry_run:
        for ont in onts:
            logger.info(
                "  [DRY RUN] Would rebind ONT %s (ONT-ID %d on %s) → profile %d",
                ont["serial_number"], ont["ont_id"], ont["fsp"], profile_id,
            )
            stats["bound"] += 1
        return stats

    # Open single SSH session to OLT
    try:
        transport, channel, _policy = core._open_shell(olt)
    except Exception as exc:
        logger.error("SSH connection failed to %s: %s", olt.name, exc)
        stats["errors"] = len(onts)
        return stats

    try:
        channel.send("enable\n")
        core._read_until_prompt(channel, r"#\s*$", timeout_sec=5)
        config_prompt = r"[#)]\s*$"
        core._run_huawei_cmd(channel, "config", prompt=config_prompt)

        current_frame_slot: str | None = None

        for ont in onts:
            fs = ont["frame_slot"]
            port_num = ont["port_num"]
            ont_id = ont["ont_id"]
            serial = ont["serial_number"]

            # Enter interface context if needed (minimize quit/re-enter)
            if fs != current_frame_slot:
                if current_frame_slot is not None:
                    core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
                core._run_huawei_cmd(channel, f"interface gpon {fs}", prompt=config_prompt)
                current_frame_slot = fs

            # Bind TR-069 profile
            cmd = f"ont tr069-server-config {port_num} {ont_id} profile-id {profile_id}"
            output = core._run_huawei_cmd(channel, cmd, prompt=config_prompt)

            if core.is_error_output(output):
                logger.warning(
                    "  FAILED bind for ONT %s (ID %d): %s",
                    serial, ont_id, output.strip()[-120:],
                )
                stats["errors"] += 1
                continue

            # Reset ONT to trigger re-registration
            reset_out = core._run_huawei_cmd(
                channel, f"ont reset {port_num} {ont_id}", prompt=r"[#)]\s*$|y/n"
            )
            if "y/n" in reset_out:
                channel.send("y\n")
                core._read_until_prompt(channel, config_prompt, timeout_sec=8)

            logger.info(
                "  Rebound ONT %s (ID %d on %s) → profile %d",
                serial, ont_id, ont["fsp"], profile_id,
            )
            stats["bound"] += 1

            if delay_between > 0:
                time.sleep(delay_between)

        # Exit interface + config modes
        if current_frame_slot is not None:
            core._run_huawei_cmd(channel, "quit", prompt=config_prompt)
        core._run_huawei_cmd(channel, "quit", prompt=config_prompt)

    except Exception as exc:
        logger.error("SSH error on %s: %s", olt.name, exc)
        stats["errors"] += 1
    finally:
        transport.close()

    return stats


def run(
    db: Session,
    *,
    profile_id: int,
    olt_name: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict[str, int]:
    """Run the bulk rebind across OLTs.

    Args:
        db: Database session.
        profile_id: Target TR-069 OLT profile ID.
        olt_name: If set, only rebind ONTs on this OLT.
        dry_run: If True, log commands but don't execute.
        limit: Max ONTs per OLT (for staged rollout).

    Returns:
        Aggregate stats dict.
    """
    from app.models.network import OLTDevice, OntAssignment, OntUnit, PonPort

    # Build OLT filter
    olt_stmt = select(OLTDevice).where(OLTDevice.mgmt_ip.isnot(None))
    if olt_name:
        olt_stmt = olt_stmt.where(OLTDevice.name == olt_name)
    olts = db.scalars(olt_stmt).all()

    if not olts:
        logger.error("No OLTs found%s", f" matching '{olt_name}'" if olt_name else "")
        return {"bound": 0, "skipped": 0, "errors": 0}

    totals = {"bound": 0, "skipped": 0, "errors": 0}

    for olt in olts:
        # Find ONTs on this OLT via active assignments
        stmt = (
            select(
                OntUnit.serial_number,
                OntUnit.board,
                OntUnit.port,
                OntUnit.external_id,
            )
            .join(OntAssignment, OntAssignment.ont_unit_id == OntUnit.id)
            .join(PonPort, PonPort.id == OntAssignment.pon_port_id)
            .where(
                PonPort.olt_id == olt.id,
                OntAssignment.active.is_(True),
                OntUnit.online_status == "online",
            )
            .order_by(OntUnit.board, OntUnit.port, OntUnit.external_id)
        )
        if limit:
            stmt = stmt.limit(limit)

        rows = db.execute(stmt).fetchall()

        # Parse ONT-IDs and filter out unparseable ones
        batch: list[dict] = []
        for r in rows:
            ont_id = _parse_ont_id_from_external(r.external_id)
            fsp = _build_fsp(r.board, r.port)
            if ont_id is None or fsp is None:
                logger.warning(
                    "  Skipping %s — cannot resolve ONT-ID (external_id=%s, board=%s, port=%s)",
                    r.serial_number, r.external_id, r.board, r.port,
                )
                totals["skipped"] += 1
                continue

            parts = fsp.split("/")
            batch.append({
                "serial_number": r.serial_number,
                "fsp": fsp,
                "ont_id": ont_id,
                "frame_slot": f"{parts[0]}/{parts[1]}",
                "port_num": parts[2],
            })

        logger.info(
            "%s: %d ONTs online, %d with valid ONT-ID, %d skipped",
            olt.name, len(rows), len(batch), len(rows) - len(batch),
        )

        if not batch:
            continue

        # Group by frame_slot for efficient SSH session usage
        batch.sort(key=lambda x: (x["frame_slot"], x["ont_id"]))

        stats = _rebind_batch_on_olt(
            olt, batch, profile_id, dry_run=dry_run,
        )
        for k in totals:
            totals[k] += stats.get(k, 0)

    return totals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk TR-069 profile rebind for SmartOLT → GenieACS migration",
    )
    parser.add_argument(
        "--profile-id", type=int, required=True,
        help="Target TR-069 OLT profile ID (e.g., 2 for GenieACS)",
    )
    parser.add_argument(
        "--olt", type=str, default=None,
        help="Only rebind ONTs on this OLT (exact name match)",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually execute the rebind (default is dry-run)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max ONTs per OLT (for staged rollout)",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    mode = "DRY RUN" if dry_run else "EXECUTE"
    logger.info("=== Bulk TR-069 Rebind [%s] ===", mode)
    logger.info("Target profile ID: %d", args.profile_id)
    if args.olt:
        logger.info("OLT filter: %s", args.olt)
    if args.limit:
        logger.info("Limit: %d per OLT", args.limit)
    logger.info("")

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        totals = run(
            db,
            profile_id=args.profile_id,
            olt_name=args.olt,
            dry_run=dry_run,
            limit=args.limit,
        )
        logger.info("")
        logger.info("=== Summary ===")
        logger.info("Bound:   %d", totals["bound"])
        logger.info("Skipped: %d", totals["skipped"])
        logger.info("Errors:  %d", totals["errors"])

        if dry_run:
            logger.info("")
            logger.info("This was a DRY RUN. Add --execute to actually rebind.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
