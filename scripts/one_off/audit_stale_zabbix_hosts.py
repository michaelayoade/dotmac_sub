"""Audit (and optionally disable) stale, decommissioned Zabbix hosts.

Long-dead hosts that were never cleaned out of Zabbix pollute every monitoring
dashboard — they read "down/problem" forever and drag the totals down (see the
2026-06-26 investigation: ~171 hosts unreachable >30 days, 0 customers, not
matched to any NAS/OLT). They are decommission candidates, not outages.

A host is a candidate when ALL hold:
  * it has an active "Unavailable by ICMP" trigger older than --min-days,
  * its NetworkDevice has 0 current subscribers,
  * it is not matched to a NAS/OLT (matched_device_type is NULL).

Dry-run by default (writes nothing). ``--execute`` DISABLES each candidate host
in Zabbix (host.update status=1) — reversible, never deletes. Run inside a
container that can reach Zabbix.

  python -m scripts.one_off.audit_stale_zabbix_hosts              # dry-run
  python -m scripts.one_off.audit_stale_zabbix_hosts --min-days 30
  python -m scripts.one_off.audit_stale_zabbix_hosts --execute    # disable them
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass


@dataclass
class Candidate:
    hostid: str
    name: str
    mgmt_ip: str | None
    days_down: float
    subs: int
    matched: str | None


def select_stale_candidates(
    devices: dict[str, dict],
    unreachable_age_days: dict[str, float],
    *,
    min_days: float,
) -> list[Candidate]:
    """Pure selection: a host is stale-decommission-candidate when it is
    unreachable for >= min_days, has 0 subscribers, and is unmatched.

    ``devices``: hostid -> {name, mgmt_ip, subs, matched}.
    ``unreachable_age_days``: hostid -> age in days of its unreachable trigger.
    """
    out: list[Candidate] = []
    for hostid, age in unreachable_age_days.items():
        if age < min_days:
            continue
        meta = devices.get(hostid)
        if meta is None:
            continue
        if int(meta.get("subs") or 0) != 0:
            continue
        if meta.get("matched"):  # matched to NAS/OLT -> keep (real infra)
            continue
        out.append(
            Candidate(
                hostid=hostid,
                name=meta.get("name") or hostid,
                mgmt_ip=meta.get("mgmt_ip"),
                days_down=age,
                subs=0,
                matched=None,
            )
        )
    out.sort(key=lambda c: c.days_down, reverse=True)
    return out


def _load_devices(db) -> dict[str, dict]:
    from sqlalchemy import text

    rows = db.execute(
        text(
            "select zabbix_hostid, name, mgmt_ip, "
            "coalesce(current_subscriber_count,0), matched_device_type "
            "from network_devices "
            "where is_active and source='zabbix_reconcile' "
            "and zabbix_hostid is not null"
        )
    ).fetchall()
    return {
        str(r[0]): {"name": r[1], "mgmt_ip": r[2], "subs": r[3], "matched": r[4]}
        for r in rows
    }


def _unreachable_ages(client, host_ids: list[str], now: float) -> dict[str, float]:
    ages: dict[str, float] = {}

    def chunks(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i : i + n]

    for ch in chunks(host_ids, 200):
        for t in client.get_triggers(host_ids=ch, active_only=True, limit=100000):
            if "Unavailable by ICMP" not in t.get("description", ""):
                continue
            lc = int(t.get("lastchange") or 0)
            if lc <= 0:
                continue
            age = (now - lc) / 86400
            for h in t.get("hosts", []):
                hid = str(h.get("hostid"))
                ages[hid] = max(ages.get(hid, 0.0), age)
    return ages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-days", type=float, default=30.0, help="Unreachable threshold (days)."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Disable candidate hosts in Zabbix. Default: dry-run.",
    )
    args = parser.parse_args()

    from app.db import SessionLocal
    from app.services.zabbix import ZabbixClient

    db = SessionLocal()
    try:
        devices = _load_devices(db)
    finally:
        db.close()
    client = ZabbixClient.from_env()
    ages = _unreachable_ages(client, list(devices), time.time())
    candidates = select_stale_candidates(devices, ages, min_days=args.min_days)

    print(
        f"Stale Zabbix host audit: {len(candidates)} candidate(s) "
        f"(unreachable >= {args.min_days:g}d, 0 subscribers, unmatched)\n"
    )
    for c in candidates:
        print(
            f"  {c.days_down:7.1f}d  {(c.mgmt_ip or '-'):16}  {c.name[:44]}  "
            f"[hostid={c.hostid}]"
        )

    if not args.execute:
        print(
            f"\nDRY-RUN — nothing changed. Re-run with --execute to disable "
            f"these {len(candidates)} host(s) in Zabbix."
        )
        return 0

    print(f"\n--execute: disabling {len(candidates)} host(s) in Zabbix...")
    ok = err = 0
    for c in candidates:
        try:
            client.update_host(host_id=c.hostid, status=1)  # 1 = disabled
            ok += 1
        except Exception as exc:  # noqa: BLE001
            err += 1
            print(f"  ERROR disabling {c.name} ({c.hostid}): {exc}")
    print(f"Done: disabled={ok} errors={err}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
