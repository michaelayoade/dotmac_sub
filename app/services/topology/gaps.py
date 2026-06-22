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

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.customer_path import (
    GAP_NO_BASESTATION,
    GAP_NO_NODE,
    GAP_NO_ONT,
)
from app.services.topology.zabbix_reconcile import SOURCE

DEFAULT_TABLE_PER_PAGE = 50


@dataclass
class TopologyGaps:
    unmatched_nodes: list[NetworkDevice] = field(default_factory=list)
    unmatched_node_total_count: int = 0
    unmatched_node_page: int = 1
    unmatched_node_per_page: int = DEFAULT_TABLE_PER_PAGE
    subscription_gaps: list[dict] = field(default_factory=list)  # {id, gap}
    subscription_gap_total_count: int = 0
    subscription_gap_page: int = 1
    subscription_gap_per_page: int = DEFAULT_TABLE_PER_PAGE
    active_subscriptions: int = 0
    resolved_complete: int = 0

    @property
    def unmatched_node_count(self) -> int:
        return self.unmatched_node_total_count

    @property
    def unmatched_node_total_pages(self) -> int:
        return _total_pages(
            self.unmatched_node_total_count, self.unmatched_node_per_page
        )

    @property
    def subscription_gap_total_pages(self) -> int:
        return _total_pages(
            self.subscription_gap_total_count, self.subscription_gap_per_page
        )

    @property
    def subscription_gap_count(self) -> int:
        return self.subscription_gap_total_count

    @property
    def subscription_gap_display_count(self) -> int:
        return len(self.subscription_gaps)

    @property
    def subscription_gaps_truncated(self) -> bool:
        return self.subscription_gap_total_count > len(self.subscription_gaps)

    @property
    def match_rate(self) -> float:
        if not self.active_subscriptions:
            return 0.0
        return self.resolved_complete / self.active_subscriptions


def _clamp_page(page: int) -> int:
    return max(page, 1)


def _clamp_per_page(per_page: int) -> int:
    return min(max(per_page, 10), 200)


def _total_pages(total: int, per_page: int) -> int:
    if total <= 0:
        return 1
    return ((total - 1) // per_page) + 1


def _page_slice(rows: list, *, page: int, per_page: int) -> list:
    offset = (page - 1) * per_page
    return rows[offset : offset + per_page]


def _active_assignment_query():
    return (
        select(
            OntAssignment.subscriber_id,
            OntAssignment.service_address_id,
            OntAssignment.ont_unit_id,
        )
        .where(
            OntAssignment.subscriber_id.is_not(None),
            OntAssignment.active.is_(True),
        )
        .order_by(OntAssignment.id)
    )


def _device_node_state(
    db: Session, *, device_type: str, device_ids: set
) -> dict[object, tuple[bool, bool]]:
    """Return {device_id: (has_node, has_node_with_existing_pop_site)}."""
    if not device_ids:
        return {}

    rows = db.execute(
        select(
            NetworkDevice.matched_device_id,
            NetworkDevice.pop_site_id,
        ).where(
            NetworkDevice.matched_device_type == device_type,
            NetworkDevice.matched_device_id.in_(device_ids),
        )
    ).all()
    pop_site_ids = {row.pop_site_id for row in rows if row.pop_site_id is not None}
    existing_pop_site_ids = set()
    if pop_site_ids:
        existing_pop_site_ids = set(
            db.execute(select(PopSite.id).where(PopSite.id.in_(pop_site_ids))).scalars()
        )

    state: dict[object, tuple[bool, bool]] = {}
    for row in rows:
        has_complete_node = row.pop_site_id in existing_pop_site_ids
        has_node, has_complete = state.get(row.matched_device_id, (False, False))
        state[row.matched_device_id] = (
            True,
            has_complete or has_complete_node,
        )
    return state


def _subscription_gap_rows(db: Session) -> tuple[int, list[dict]]:
    # NOTE: This is a batched (set-based) reimplementation of the per-subscription
    # gap classification in ``resolve_customer_path`` (app/services/topology/
    # customer_path.py), avoiding an N+1 across all active subscriptions. The two
    # MUST stay in sync — ``resolve_customer_path`` remains the canonical reader
    # for a single subscription's path; this function must produce the same
    # GAP_NO_ONT / GAP_NO_NODE / GAP_NO_BASESTATION verdict in aggregate.
    active_subs = db.execute(
        select(
            Subscription.id,
            Subscription.subscriber_id,
            Subscription.service_address_id,
            Subscription.provisioning_nas_device_id,
        )
        .where(Subscription.status == SubscriptionStatus.active)
        .order_by(Subscription.id)
    ).all()
    if not active_subs:
        return 0, []

    subscriber_ids = {row.subscriber_id for row in active_subs}
    assignment_rows = db.execute(
        _active_assignment_query().where(
            OntAssignment.subscriber_id.in_(subscriber_ids)
        )
    ).all()
    assignments_by_subscriber: defaultdict[object, list] = defaultdict(list)
    assignments_by_subscriber_address: defaultdict[tuple[object, object], list] = (
        defaultdict(list)
    )
    ont_ids = set()
    for row in assignment_rows:
        assignments_by_subscriber[row.subscriber_id].append(row.ont_unit_id)
        if row.service_address_id is not None:
            assignments_by_subscriber_address[
                (row.subscriber_id, row.service_address_id)
            ].append(row.ont_unit_id)
        ont_ids.add(row.ont_unit_id)

    ont_to_olt: dict[object, object] = {}
    if ont_ids:
        ont_to_olt = {
            row.id: row.olt_device_id
            for row in db.execute(
                select(OntUnit.id, OntUnit.olt_device_id).where(OntUnit.id.in_(ont_ids))
            )
            if row.olt_device_id is not None
        }

    olt_ids = set(ont_to_olt.values())
    existing_olt_ids = set()
    if olt_ids:
        existing_olt_ids = set(
            db.execute(select(OLTDevice.id).where(OLTDevice.id.in_(olt_ids))).scalars()
        )
    nas_ids = {
        row.provisioning_nas_device_id
        for row in active_subs
        if row.provisioning_nas_device_id is not None
    }
    existing_nas_ids = set()
    if nas_ids:
        existing_nas_ids = set(
            db.execute(select(NasDevice.id).where(NasDevice.id.in_(nas_ids))).scalars()
        )
    olt_node_state = _device_node_state(db, device_type="olt", device_ids=olt_ids)
    nas_node_state = _device_node_state(db, device_type="nas", device_ids=nas_ids)

    gap_rows: list[dict] = []
    for row in active_subs:
        assignment_ont_ids = assignments_by_subscriber[row.subscriber_id]
        if row.service_address_id is not None:
            address_assignment_ids = assignments_by_subscriber_address[
                (row.subscriber_id, row.service_address_id)
            ]
            if address_assignment_ids:
                assignment_ont_ids = address_assignment_ids

        selected_ont_id = assignment_ont_ids[0] if assignment_ont_ids else None
        has_access_device = selected_ont_id is not None or (
            row.provisioning_nas_device_id in existing_nas_ids
        )
        has_node = False
        has_complete_path = False

        if selected_ont_id is not None:
            olt_id = ont_to_olt.get(selected_ont_id)
            if olt_id is None:
                has_access_device = True
            elif olt_id in existing_olt_ids:
                node_exists, complete_node = olt_node_state.get(olt_id, (False, False))
                has_node = node_exists
                has_complete_path = complete_node
            else:
                has_access_device = True

        if (
            selected_ont_id is None
            and row.provisioning_nas_device_id in existing_nas_ids
        ):
            node_exists, complete_node = nas_node_state.get(
                row.provisioning_nas_device_id, (False, False)
            )
            has_node = node_exists
            has_complete_path = complete_node

        gap = None
        if not has_access_device:
            gap = GAP_NO_ONT
        elif not has_node:
            gap = GAP_NO_NODE
        elif not has_complete_path:
            gap = GAP_NO_BASESTATION
        if gap:
            gap_rows.append({"id": row.id, "gap": gap})

    return len(active_subs), gap_rows


def topology_gaps(
    db: Session,
    *,
    node_page: int = 1,
    node_per_page: int = DEFAULT_TABLE_PER_PAGE,
    gap_page: int = 1,
    gap_per_page: int = DEFAULT_TABLE_PER_PAGE,
) -> TopologyGaps:
    node_page = _clamp_page(node_page)
    gap_page = _clamp_page(gap_page)
    node_per_page = _clamp_per_page(node_per_page)
    gap_per_page = _clamp_per_page(gap_per_page)
    gaps = TopologyGaps(
        unmatched_node_page=node_page,
        unmatched_node_per_page=node_per_page,
        subscription_gap_page=gap_page,
        subscription_gap_per_page=gap_per_page,
    )

    unmatched_query = db.query(NetworkDevice).filter(
        NetworkDevice.source == SOURCE,
        NetworkDevice.matched_device_id.is_(None),
        NetworkDevice.is_active.is_(True),
    )
    gaps.unmatched_node_total_count = unmatched_query.count()
    node_offset = (node_page - 1) * node_per_page
    gaps.unmatched_nodes = (
        unmatched_query.order_by(NetworkDevice.name)
        .limit(node_per_page)
        .offset(node_offset)
        .all()
    )

    gaps.active_subscriptions, all_subscription_gaps = _subscription_gap_rows(db)
    gaps.subscription_gap_total_count = len(all_subscription_gaps)
    gaps.resolved_complete = max(
        gaps.active_subscriptions - gaps.subscription_gap_total_count, 0
    )
    gaps.subscription_gaps = _page_slice(
        all_subscription_gaps, page=gap_page, per_page=gap_per_page
    )

    return gaps
