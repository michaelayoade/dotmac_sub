"""Preview the LLDP topology poll against the live fleet — no DB writes.

Runs the real poll (reads each NAS's /ip/neighbor) in an uncommitted session and
prints the stats + the directed edges it WOULD create, with device names, then
rolls back. Use to verify the expected infra adjacencies before enabling the
scheduled poll.

    python -m scripts.one_off.lldp_poll_dryrun

Run on a host with reachability to the MikroTik NAS fleet.
"""

from __future__ import annotations

import json

from app.db import SessionLocal
from app.models.network_monitoring import NetworkDevice, NetworkTopologyLink
from app.services.topology.lldp_poller import SOURCE, poll_all


def main() -> int:
    db = SessionLocal()
    try:
        stats = poll_all(db)  # writes to the session only
        links = (
            db.query(NetworkTopologyLink)
            .filter(
                NetworkTopologyLink.source == SOURCE,
                NetworkTopologyLink.is_active.is_(True),
            )
            .all()
        )
        names = {d.id: d.name for d in db.query(NetworkDevice).all()}
        print("LLDP topology poll — DRY-RUN (no writes)")
        print(json.dumps(stats, indent=2, default=str))
        print(f"\n{len(links)} directed edges:")
        for link in sorted(
            links,
            key=lambda link_: names.get(link_.source_device_id, ""),
        ):
            a = names.get(link.source_device_id, str(link.source_device_id))
            b = names.get(link.target_device_id, str(link.target_device_id))
            medium = getattr(link.medium, "value", link.medium)
            print(f"  {a}  <->  {b}   ({medium})")
    finally:
        db.rollback()  # discard — nothing persisted
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
