"""Preview (or apply) the topology reconcile against the real Zabbix + DB.

Phase 1 of the customer-path feature. By default this is READ-ONLY: it computes
what the reconcile WOULD do (match-merge pop_sites + network_devices) and prints
the summary, so the first run against the populated tables (461 nodes / 23
pop_sites / orphaned legacy rows) can be reviewed before any write.

    python -m scripts.one_off.topology_reconcile_dryrun           # dry-run (default)
    python -m scripts.one_off.topology_reconcile_dryrun --apply   # write + commit

Run on a host with the prod Zabbix + DB env (uses ZabbixClient.from_env()).
"""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.topology.zabbix_reconcile import reconcile
from app.services.zabbix import ZabbixClient, ZabbixClientError, zabbix_configured


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write + commit (default is a read-only dry-run).",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    if not zabbix_configured():
        print("Zabbix is not configured (token/url missing) — aborting.")
        return 2

    db = SessionLocal()
    try:
        client = ZabbixClient.from_env()
        result = reconcile(db, client, dry_run=dry_run)
        if args.apply:
            db.commit()
    except ZabbixClientError as exc:
        db.rollback()
        print(f"Zabbix unavailable: {exc}")
        return 2
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    mode = "APPLIED" if args.apply else "DRY-RUN (no writes)"
    print(f"Topology reconcile — {mode}")
    print(json.dumps(result, indent=2, default=str))

    nd = result["network_devices"]
    total_matched = nd["device_matched"]
    total_seen = total_matched + nd["unmatched"] + nd["ambiguous"]
    if total_seen:
        rate = 100.0 * total_matched / total_seen
        print(
            f"\nHost->device match: {total_matched}/{total_seen} ({rate:.1f}%); "
            f"unmatched={nd['unmatched']} ambiguous={nd['ambiguous']} "
            f"pruned={nd['pruned']} duplicate_host={nd['duplicate_host']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
