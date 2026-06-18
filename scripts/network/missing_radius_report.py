#!/usr/bin/env python
"""Read-only report: active subscriptions missing a RADIUS auth path.

A customer must not be billed by automation while they cannot authenticate.
This enumerates active subscriptions whose login is NOT present in the external
``radcheck`` (the `active_subscription_missing_radius` launch-blocking gauge),
with the context needed to decide each case, and a proposed classification:

  - provision   : a real active customer — create the RADIUS auth path.
  - qa_exclude  : a QA/test login — should be excluded from the launch gate.
  - manual_review: missing data / ambiguous — a human decides.

Writes nothing. See docs/BILLING_AUTOMATION_LAUNCH_RUNBOOK.md (Step 2).

Usage:
    docker compose exec -T -e PYTHONPATH=/app app \
        python scripts/network/missing_radius_report.py
"""

from __future__ import annotations

import sys

from sqlalchemy import Column, String, select

from app.db import SessionLocal
from app.models.catalog import (
    RadiusProfile,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services.radius import (
    _active_external_sync_configs,
    _external_radius_table,
    _get_external_engine,
)

COLUMNS = [
    "subscription_id",
    "subscriber_id",
    "login",
    "offer",
    "radius_profile",
    "subscription_status",
    "subscriber_status",
    "ipv4_address",
    "ip_assignment",
    "radcheck_present",
    "radreply_present",
    "expected_radius_action",
    "classification",
]


def _radius_presence(db, logins: list[str]) -> tuple[set[str], set[str]]:
    """Return (logins-in-radcheck, logins-in-radreply) across external configs."""
    in_radcheck: set[str] = set()
    in_radreply: set[str] = set()
    for config in _active_external_sync_configs(db):
        try:
            engine = _get_external_engine(config["db_url"])
            radcheck = _external_radius_table(
                config.get("radcheck_table", "radcheck"),
                Column("username", String),
            )
            radreply = _external_radius_table(
                config.get("radreply_table", "radreply"),
                Column("username", String),
            )
            with engine.connect() as conn:
                in_radcheck |= set(
                    conn.execute(
                        select(radcheck.c.username)
                        .distinct()
                        .where(radcheck.c.username.in_(logins))
                    ).scalars()
                )
                in_radreply |= set(
                    conn.execute(
                        select(radreply.c.username)
                        .distinct()
                        .where(radreply.c.username.in_(logins))
                    ).scalars()
                )
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: external RADIUS read failed: {exc}")
    return in_radcheck, in_radreply


def _is_qa(login: str) -> bool:
    low = login.lower()
    return low.startswith(("qa", "test", "e2e", "demo")) or "qa-test" in low


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(
                Subscription.id,
                Subscription.subscriber_id,
                Subscription.login,
                Subscription.ipv4_address,
                Subscription.status,
                Subscription.radius_profile_id,
                Subscription.offer_id,
            ).where(Subscription.status == SubscriptionStatus.active)
        ).all()
        logins = sorted({r.login.strip() for r in rows if r.login and r.login.strip()})
        in_radcheck, in_radreply = _radius_presence(db, logins)

        report: list[dict] = []
        for r in rows:
            login = (r.login or "").strip()
            if not login or login in in_radcheck:
                continue  # not missing
            subscriber = db.get(Subscriber, r.subscriber_id)
            offer_name = ""
            if r.offer_id:
                from app.models.catalog import CatalogOffer

                o = db.get(CatalogOffer, r.offer_id)
                offer_name = o.name if o else ""
            profile_name = ""
            if r.radius_profile_id:
                p = db.get(RadiusProfile, r.radius_profile_id)
                profile_name = p.name if p else ""
            assign = db.execute(
                select(IPv4Address.address)
                .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
                .where(IPAssignment.is_active.is_(True))
                .where(IPAssignment.ip_version == IPVersion.ipv4)
                .where(IPAssignment.subscriber_id == r.subscriber_id)
            ).scalar()

            qa = _is_qa(login)
            classification = "qa_exclude" if qa else "provision"
            if not offer_name and not qa:
                classification = "manual_review"
            action = (
                "exclude from launch gate (document)"
                if qa
                else "provision radcheck (+radreply for static IP) from current state"
            )
            report.append(
                {
                    "subscription_id": str(r.id),
                    "subscriber_id": str(r.subscriber_id),
                    "login": login,
                    "offer": offer_name,
                    "radius_profile": profile_name,
                    "subscription_status": getattr(r.status, "value", str(r.status)),
                    "subscriber_status": getattr(
                        subscriber.status, "value", str(subscriber.status)
                    )
                    if subscriber
                    else "",
                    "ipv4_address": r.ipv4_address or "",
                    "ip_assignment": assign or "",
                    "radcheck_present": login in in_radcheck,
                    "radreply_present": login in in_radreply,
                    "expected_radius_action": action,
                    "classification": classification,
                }
            )

        print(f"active subs missing radcheck: {len(report)}\n")
        for item in report:
            print(f"--- {item['login']} [{item['classification']}] ---")
            for col in COLUMNS:
                print(f"  {col:22s}: {item[col]}")
            print()
        from collections import Counter

        print("by classification:", dict(Counter(i["classification"] for i in report)))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
