#!/usr/bin/env python
"""Audit how many subscribers actually get LIVE bandwidth, once, as JSON.

Read-only. The customer web/mobile "Live Bandwidth" surfaces and the admin
per-customer live read all depend on the MikroTik bandwidth poller producing a
``bandwidth_samples`` row for the subscription. The poller only emits a sample
when it can map a router ``/queue/simple`` entry back to the subscription —
either via an explicit ``queue_mappings`` row (written at turn-up, not kept in
sync) or the login fallback. A subscriber with no simple queue / no mapping
produces no sample and the UI degrades to "Connecting…" / no figure.

This quantifies the gap before we promote "Live" prominently:

  * empirical coverage — active subs with a fresh ``bandwidth_samples`` row in
    the last N minutes (ground truth: the poller is succeeding for them now);
  * eligibility — active subs that have a provisioning NAS, an explicit queue
    mapping, and/or a login for the fallback;
  * a per-NAS breakdown so a single mis-provisioned router is obvious;
  * stale/orphan samples whose subscription is no longer live.

Usage (inside the app container so the DB resolves):

    docker compose exec app python scripts/one_off/audit_live_bandwidth_coverage.py
    docker compose exec app python scripts/one_off/audit_live_bandwidth_coverage.py --window-minutes 15
    docker compose exec app python scripts/one_off/audit_live_bandwidth_coverage.py --json
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func

from app.db import SessionLocal
from app.models.bandwidth import BandwidthSample, QueueMapping
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.services.billing_settings import LIVE_SERVICE_STATUSES


def _pct(part: int, whole: int) -> float:
    return round((part / whole) * 100, 1) if whole else 0.0


def audit_live_bandwidth_coverage(session, *, window_minutes: int = 15) -> dict:
    cutoff = datetime.now(UTC) - timedelta(minutes=window_minutes)

    # Subscription ids by relevant cohort.
    live_statuses = set(LIVE_SERVICE_STATUSES)
    rows = session.query(
        Subscription.id,
        Subscription.status,
        Subscription.provisioning_nas_device_id,
        Subscription.login,
    ).all()

    active_ids: set = set()
    live_count = 0
    active_with_nas: set = set()
    active_with_login: set = set()
    nas_of: dict = {}
    for sub_id, status, nas_id, login in rows:
        if status in live_statuses:
            live_count += 1
        if status == SubscriptionStatus.active:
            active_ids.add(sub_id)
            nas_of[sub_id] = nas_id
            if nas_id is not None:
                active_with_nas.add(sub_id)
            if login and str(login).strip():
                active_with_login.add(sub_id)

    # Explicit queue mappings (subscription level — keyed per device in the
    # table, but the poller only needs one live queue per sub).
    mapping_sub_ids: set = {
        sid
        for (sid,) in session.query(QueueMapping.subscription_id)
        .filter(QueueMapping.is_active.is_(True))
        .distinct()
        .all()
    }

    # Empirical: subs with a fresh sample in the window (the poller is working).
    recent_sample_ids: set = {
        sid
        for (sid,) in session.query(BandwidthSample.subscription_id)
        .filter(BandwidthSample.sample_at >= cutoff)
        .distinct()
        .all()
    }

    active_live = active_ids & recent_sample_ids
    active_mapped = active_ids & mapping_sub_ids
    # Live AND has a provisioning NAS — the proper subset for the
    # live-vs-NAS-assigned ratio (a sub can have a fresh sample yet a NULL
    # provisioning NAS, since samples are keyed by the sample's device).
    active_live_with_nas = active_live & active_with_nas
    # Orphan samples: fresh data for a sub that is not currently active.
    orphan_sample_ids = recent_sample_ids - active_ids

    # Per-NAS breakdown over active subs.
    nas_names: dict = {
        nid: name for nid, name in session.query(NasDevice.id, NasDevice.name).all()
    }
    per_nas: dict = {}
    for sid in active_ids:
        nid = nas_of.get(sid)
        bucket = per_nas.setdefault(
            nid,
            {"active": 0, "live": 0, "mapped": 0},
        )
        bucket["active"] += 1
        if sid in recent_sample_ids:
            bucket["live"] += 1
        if sid in mapping_sub_ids:
            bucket["mapped"] += 1

    nas_breakdown = sorted(
        (
            {
                "nas_device_id": str(nid) if nid else None,
                "nas_name": nas_names.get(nid, "(no NAS assigned)"),
                "active": b["active"],
                "live": b["live"],
                "mapped": b["mapped"],
                "live_pct": _pct(b["live"], b["active"]),
            }
            for nid, b in per_nas.items()
        ),
        key=lambda r: (-r["active"], r["nas_name"] or ""),
    )

    total_recent_samples = (
        session.query(func.count(BandwidthSample.id))
        .filter(BandwidthSample.sample_at >= cutoff)
        .scalar()
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_minutes": window_minutes,
        "totals": {
            "live_service_subs": live_count,
            "active_subs": len(active_ids),
            "active_with_nas": len(active_with_nas),
            "active_with_queue_mapping": len(active_mapped),
            "active_with_login": len(active_with_login),
            "active_live_now": len(active_live),
            "rows_in_window": int(total_recent_samples or 0),
            "orphan_sample_subs": len(orphan_sample_ids),
        },
        "coverage_pct": {
            "live_now_vs_active": _pct(len(active_live), len(active_ids)),
            "live_now_vs_active_with_nas": _pct(
                len(active_live_with_nas), len(active_with_nas)
            ),
            "mapped_vs_active": _pct(len(active_mapped), len(active_ids)),
            "nas_assigned_vs_active": _pct(len(active_with_nas), len(active_ids)),
        },
        "per_nas": nas_breakdown,
    }


def _print_summary(result: dict) -> None:
    t = result["totals"]
    c = result["coverage_pct"]
    print(f"\nLive bandwidth coverage  (window: last {result['window_minutes']} min)")
    print("=" * 64)
    print(f"  active subscriptions ............. {t['active_subs']}")
    print(
        f"  ├─ live now (fresh sample) ....... {t['active_live_now']:>6}  "
        f"({c['live_now_vs_active']}% of active)"
    )
    print(
        f"  ├─ have a provisioning NAS ....... {t['active_with_nas']:>6}  "
        f"({c['nas_assigned_vs_active']}% of active)"
    )
    print(
        f"  ├─ have a queue mapping .......... {t['active_with_queue_mapping']:>6}  "
        f"({c['mapped_vs_active']}% of active)"
    )
    print(f"  └─ have a login (fallback) ....... {t['active_with_login']:>6}")
    print(
        f"\n  live-now vs subs that even have a NAS: "
        f"{c['live_now_vs_active_with_nas']}%"
    )
    print(
        f"  samples in window: {t['rows_in_window']}  |  "
        f"orphan-sample subs (not active): {t['orphan_sample_subs']}"
    )

    print("\n  Per-NAS (top by active count):")
    for r in result["per_nas"][:20]:
        print(
            f"    {r['nas_name'][:34]:<34} "
            f"active={r['active']:>5}  live={r['live']:>5}  "
            f"mapped={r['mapped']:>5}  ({r['live_pct']}% live)"
        )
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=15,
        help="Freshness window for 'live now' samples (default: 15).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the JSON result (no human summary).",
    )
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = audit_live_bandwidth_coverage(
            session, window_minutes=args.window_minutes
        )
    finally:
        session.close()

    if not args.json:
        _print_summary(result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
