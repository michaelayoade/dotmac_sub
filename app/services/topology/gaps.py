"""Topology-gaps report + match-rate (Phase 1, Task 8).

Surfaces what the reconcile could not resolve so it can be fixed instead of
silently lost:
- Zabbix-synced nodes with no confident provisioning-device match (unmatched or
  ambiguous both land as matched_device_id IS NULL in Phase 1).
- Active subscriptions whose resolve_customer_path returns a gap.

The match-rate (% of active subscriptions resolving to a complete
ONT -> device -> basestation path) is the Phase 1 exit metric, and doubles as
the empirical answer to "how many customers have no resolvable path" (open
decision C re: wireless).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice
from app.services.topology.customer_path import resolve_customer_path
from app.services.topology.zabbix_reconcile import SOURCE


@dataclass
class TopologyGaps:
    unmatched_nodes: list[NetworkDevice] = field(default_factory=list)
    subscription_gaps: list[dict] = field(default_factory=list)  # {id, gap}
    active_subscriptions: int = 0
    resolved_complete: int = 0

    @property
    def unmatched_node_count(self) -> int:
        return len(self.unmatched_nodes)

    @property
    def subscription_gap_count(self) -> int:
        return len(self.subscription_gaps)

    @property
    def match_rate(self) -> float:
        if not self.active_subscriptions:
            return 0.0
        return self.resolved_complete / self.active_subscriptions


def topology_gaps(db: Session) -> TopologyGaps:
    gaps = TopologyGaps()

    gaps.unmatched_nodes = (
        db.query(NetworkDevice)
        .filter(
            NetworkDevice.source == SOURCE,
            NetworkDevice.matched_device_id.is_(None),
            NetworkDevice.is_active.is_(True),
        )
        .order_by(NetworkDevice.name)
        .all()
    )

    subs = (
        db.query(Subscription)
        .filter(Subscription.status == SubscriptionStatus.active)
        .all()
    )
    gaps.active_subscriptions = len(subs)
    for sub in subs:
        path = resolve_customer_path(db, sub)
        if path.gap:
            gaps.subscription_gaps.append({"id": sub.id, "gap": path.gap})
        else:
            gaps.resolved_complete += 1

    return gaps
