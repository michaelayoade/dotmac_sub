"""Fix HW- synthetic serial numbers by querying real serials from OLTs via SSH.

SNMP discovery creates ONTs with synthetic serials like HW-363D7BE1-0200 because
Huawei OLTs don't expose serial numbers via SNMP. This script:

1. SSHs into each OLT with HW- ONTs (handles MA5608T/MA5800/MA5600 differences)
2. Runs 'display ont info 0 all' to get real serials for all registered ONTs
3. Matches by OLT + slot + port + ONU-ID and updates the serial in our DB
4. Re-syncs GenieACS to link newly-resolvable devices

Usage:
    PYTHONPATH=. poetry run python scripts/migration/fix_hw_serials.py --dry-run
    PYTHONPATH=. poetry run python scripts/migration/fix_hw_serials.py --execute
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import text

from scripts.migration.db_connections import dotmac_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fix_hw_serials")


@dataclass
class Stats:
    olts_queried: int = 0
    olts_failed: int = 0
    onts_found: int = 0
    serials_fixed: int = 0
    duplicate_skipped: int = 0
    no_match: int = 0
    genieacs_relinked: int = 0
    errors: list[str] = field(default_factory=list)


def fix_serials(db, *, dry_run: bool, stats: Stats) -> None:
    """Query each OLT for real serials and update HW- ONTs."""
    from app.models.network import OLTDevice
    from app.services.network.olt_ssh import get_registered_ont_serials

    # Get all OLTs with HW- ONTs
    olt_rows = db.execute(
        text("""
        SELECT DISTINCT d.id, d.name
        FROM olt_devices d
        JOIN ont_units o ON o.olt_device_id = d.id
        WHERE o.serial_number LIKE 'HW-%%' AND o.is_active = true AND d.is_active = true
        ORDER BY d.name
    """)
    ).fetchall()
    logger.info("Found %d OLTs with HW- ONTs", len(olt_rows))

    # Build HW- ONT lookup: (olt_id, slot, port, onu_id) → ont row
    hw_onts = db.execute(
        text("""
        SELECT id, serial_number, olt_device_id, board, port, external_id
        FROM ont_units
        WHERE serial_number LIKE 'HW-%%' AND is_active = true
    """)
    ).fetchall()

    # Key by (olt_id, port, onu_id) — skip slot since SNMP packed index
    # may decode to a different slot number than the physical OLT slot
    hw_lookup: dict[tuple[str, str, int], tuple] = {}
    for ont_id, hw_serial, olt_id, board, port_val, ext_id in hw_onts:
        if not olt_id:
            continue
        port_str = str(port_val or "").strip()
        # ONU-ID from external_id: "huawei:4194312448.5" → onu_id=5
        onu_id = -1
        ext = str(ext_id or "")
        if "." in ext:
            try:
                onu_id = int(ext.rsplit(".", 1)[-1])
            except ValueError:
                pass
        if onu_id < 0:
            continue
        key = (str(olt_id), port_str, onu_id)
        hw_lookup[key] = (ont_id, hw_serial)

    logger.info("Built lookup for %d HW- ONTs", len(hw_lookup))

    # Existing serials to avoid duplicates
    existing_serials = {
        r[0].upper()
        for r in db.execute(text("SELECT serial_number FROM ont_units")).fetchall()
        if r[0]
    }

    for olt_id, olt_name in olt_rows:
        olt = db.get(OLTDevice, str(olt_id))
        if not olt:
            continue

        logger.info("Querying %s (%s)...", olt_name, olt.model or "unknown")
        ok, msg, entries = get_registered_ont_serials(olt)
        stats.olts_queried += 1

        if not ok:
            stats.olts_failed += 1
            stats.errors.append(f"{olt_name}: {msg}")
            logger.error("  Failed: %s", msg)
            continue

        logger.info("  Got %d ONT entries from %s", len(entries), olt_name)
        stats.onts_found += len(entries)

        for entry in entries:
            # OLT SSH returns fsp="0/2/0" → port=0
            parts = entry.fsp.split("/")
            if len(parts) != 3:
                continue
            port = parts[2]

            key = (str(olt_id), port, entry.onu_id)
            hw_entry = hw_lookup.get(key)
            if not hw_entry:
                stats.no_match += 1
                continue

            ont_id, hw_serial = hw_entry

            if entry.real_serial.upper() in existing_serials:
                stats.duplicate_skipped += 1
                continue

            if dry_run:
                logger.info(
                    "  [DRY RUN] %s → %s (fsp=%s onu=%d %s)",
                    hw_serial,
                    entry.real_serial,
                    entry.fsp,
                    entry.onu_id,
                    entry.run_state,
                )
            else:
                db.execute(
                    text("UPDATE ont_units SET serial_number = :sn WHERE id = :id"),
                    {"sn": entry.real_serial, "id": ont_id},
                )
                existing_serials.add(entry.real_serial.upper())

            stats.serials_fixed += 1

    if not dry_run and stats.serials_fixed:
        db.commit()
        logger.info("Committed %d serial updates", stats.serials_fixed)


def resync_genieacs(db, *, dry_run: bool, stats: Stats) -> None:
    """Re-sync GenieACS to link newly-resolvable devices."""
    if dry_run:
        logger.info("[DRY RUN] Would re-sync GenieACS")
        return

    from app.models.tr069 import Tr069AcsServer
    from app.services.tr069 import CpeDevices

    server = db.query(Tr069AcsServer).filter(Tr069AcsServer.is_active.is_(True)).first()
    if not server:
        logger.warning("No active ACS server")
        return

    result = CpeDevices.sync_from_genieacs(db, str(server.id))
    stats.genieacs_relinked = result.get("auto_linked", 0)
    logger.info(
        "GenieACS re-sync: created=%d, updated=%d, auto-linked=%d",
        result.get("created", 0),
        result.get("updated", 0),
        stats.genieacs_relinked,
    )


def print_summary(stats: Stats) -> None:
    print("\n" + "=" * 56)
    print("  HW- Serial Fix Summary")
    print("=" * 56)
    print(f"  OLTs queried:          {stats.olts_queried}")
    print(f"  OLTs failed:           {stats.olts_failed}")
    print(f"  ONTs found on OLTs:    {stats.onts_found}")
    print(f"  Serials fixed:         {stats.serials_fixed}")
    print(f"  Duplicate skipped:     {stats.duplicate_skipped}")
    print(f"  No HW- match:          {stats.no_match}")
    print(f"  GenieACS relinked:     {stats.genieacs_relinked}")
    if stats.errors:
        print(f"\n  Errors ({len(stats.errors)}):")
        for err in stats.errors:
            print(f"    - {err}")
    print("=" * 56)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix HW- synthetic serials via OLT SSH"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    logger.info("=== %s ===", "DRY RUN" if args.dry_run else "EXECUTE")

    with dotmac_session() as db:
        try:
            stats = Stats()
            fix_serials(db, dry_run=args.dry_run, stats=stats)
            resync_genieacs(db, dry_run=args.dry_run, stats=stats)
            print_summary(stats)
        except Exception:
            logger.exception("Failed")
            db.rollback()
            sys.exit(1)


if __name__ == "__main__":
    main()
