"""Which routers serve a subscription, and are they all walled-garden ready.

Owner: ``access.captive_router_gate`` (read-only resolver).

Captive access is only safe where the router enforcing the walled garden
actually carries the module, so this gate fails closed: a subscription passes
only when at least one serving router is resolvable and EVERY serving router
is ``ready`` per ``access.walled_garden_router_readiness``.

Subscription -> NAS -> router mapping
-------------------------------------

Serving NAS candidates for one subscription are the union of:

1. ``subscriptions.provisioning_nas_device_id`` - the desired NAS assignment
   owned by ``service_intent.subscription_nas_assignment``;
2. every ``radius_active_sessions`` row bound to the subscription, plus rows
   of the same account that carry no subscription binding (accounting
   observations; inserted on Acct-Start, refreshed on interim updates and
   deleted on Acct-Stop). A row's NAS is its ``nas_device_id``, else the one
   NAS whose ``nas_ip`` or ``ip_address`` equals ``nas_ip_address``.

Each candidate NAS maps to its routers through ``routers.nas_device_id``
(active routers only).

Freshness: the assignment is current desired state. Session rows are
observations that can outlive a missed Acct-Stop; a stale row only ADDS a
candidate, which can only make the gate more restrictive, so no age filter is
applied. Readiness is snapshot-derived (``router_config_snapshots``, 48h by
default) and never contacts a router.

Fail-closed outcomes: no candidate NAS, a session whose NAS cannot be
identified (or is ambiguous), a candidate NAS without an active router, or any
serving router that is not ``ready``.

Cost: a :class:`CaptiveRouterGate` is a per-run cache. Router/NAS inventory is
loaded once, session evidence can be prefetched in bounded chunks, and the
module is rendered once per batch of routers evaluated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.catalog import NasDevice, Subscription
from app.models.radius_active_session import RadiusActiveSession
from app.models.router_management import Router
from app.services.walled_garden_router_readiness import (
    DEFAULT_MAX_SNAPSHOT_AGE,
    RoutersWalledGardenReadinessQuery,
    WalledGardenReadinessStatus,
    WalledGardenRouterReadiness,
    resolve_routers_walled_garden_readiness,
)

_PREFETCH_CHUNK = 500


class CaptiveRouterGateStatus(StrEnum):
    ready = "ready"
    router_unresolved = "router_unresolved"
    router_not_ready = "router_not_ready"


class ServingNasSource(StrEnum):
    provisioning_assignment = "provisioning_assignment"
    active_session = "active_session"


class RouterUnresolvedCause(StrEnum):
    no_serving_nas = "no_serving_nas"
    session_nas_unidentified = "session_nas_unidentified"
    nas_without_active_router = "nas_without_active_router"


@dataclass(frozen=True, slots=True)
class ServingNasEvidence:
    nas_device_id: UUID
    sources: tuple[ServingNasSource, ...]
    router_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ServingRouterReadiness:
    router_id: UUID
    router_name: str
    status: WalledGardenReadinessStatus
    snapshot_captured_at: datetime | None


@dataclass(frozen=True, slots=True)
class CaptiveRouterGateDecision:
    """Typed, explainable gate outcome for one subscription."""

    subscription_id: UUID
    status: CaptiveRouterGateStatus
    serving_nas: tuple[ServingNasEvidence, ...] = ()
    routers: tuple[ServingRouterReadiness, ...] = ()
    unresolved_causes: tuple[RouterUnresolvedCause, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.status is CaptiveRouterGateStatus.ready

    @property
    def router_ids(self) -> tuple[UUID, ...]:
        return tuple(item.router_id for item in self.routers)

    @property
    def router_names(self) -> tuple[str, ...]:
        return tuple(item.router_name for item in self.routers)


@dataclass(frozen=True, slots=True)
class _RouterRow:
    router_id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class _SessionNas:
    nas_device_id: UUID | None
    nas_ip_address: str | None


@dataclass
class CaptiveRouterGate:
    """Per-run, read-only cache of router mapping and readiness evidence.

    Build one per evaluation run (a RADIUS sweep, a policy preview/apply) and
    discard it afterwards; it never refreshes itself.
    """

    db: Session
    max_snapshot_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE
    evaluated_at: datetime | None = None
    _routers_by_nas: dict[UUID, tuple[_RouterRow, ...]] | None = field(
        default=None, init=False, repr=False
    )
    _nas_by_ip: dict[str, UUID | None] | None = field(
        default=None, init=False, repr=False
    )
    _sessions: dict[UUID, tuple[_SessionNas, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _readiness: dict[UUID, WalledGardenRouterReadiness | None] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.evaluated_at is None:
            self.evaluated_at = datetime.now(UTC)

    # -- inventory -------------------------------------------------------
    def _router_index(self) -> dict[UUID, tuple[_RouterRow, ...]]:
        if self._routers_by_nas is None:
            index: dict[UUID, list[_RouterRow]] = {}
            for router_id, name, nas_device_id in self.db.execute(
                select(Router.id, Router.name, Router.nas_device_id)
                .where(Router.nas_device_id.is_not(None))
                .where(Router.is_active.is_(True))
                .order_by(Router.name, Router.id)
            ).all():
                index.setdefault(nas_device_id, []).append(_RouterRow(router_id, name))
            self._routers_by_nas = {key: tuple(value) for key, value in index.items()}
        return self._routers_by_nas

    def _nas_ip_index(self) -> dict[str, UUID | None]:
        """IP -> NAS id; ``None`` marks an IP claimed by more than one NAS."""

        if self._nas_by_ip is None:
            index: dict[str, UUID | None] = {}
            for nas_id, nas_ip, ip_address in self.db.execute(
                select(NasDevice.id, NasDevice.nas_ip, NasDevice.ip_address)
            ).all():
                for raw in {nas_ip, ip_address}:
                    value = str(raw or "").strip()
                    if not value:
                        continue
                    if value in index and index[value] != nas_id:
                        index[value] = None
                    else:
                        index[value] = nas_id
            self._nas_by_ip = index
        return self._nas_by_ip

    # -- session evidence ------------------------------------------------
    def prefetch(self, subscriptions: Iterable[Subscription]) -> None:
        """Load session evidence for many subscriptions in bounded chunks."""

        pending = [
            item
            for item in subscriptions
            if item.id is not None and item.id not in self._sessions
        ]
        for start in range(0, len(pending), _PREFETCH_CHUNK):
            chunk = pending[start : start + _PREFETCH_CHUNK]
            self._load_sessions(chunk)

    def _load_sessions(self, subscriptions: list[Subscription]) -> None:
        by_subscription: dict[UUID, set[_SessionNas]] = {
            item.id: set() for item in subscriptions
        }
        subscription_ids = list(by_subscription)
        subscriptions_by_account: dict[UUID, list[UUID]] = {}
        for item in subscriptions:
            subscriptions_by_account.setdefault(item.subscriber_id, []).append(item.id)
        rows = self.db.execute(
            select(
                RadiusActiveSession.subscription_id,
                RadiusActiveSession.subscriber_id,
                RadiusActiveSession.nas_device_id,
                RadiusActiveSession.nas_ip_address,
            ).where(
                or_(
                    RadiusActiveSession.subscription_id.in_(subscription_ids),
                    (
                        RadiusActiveSession.subscription_id.is_(None)
                        & RadiusActiveSession.subscriber_id.in_(
                            list(subscriptions_by_account)
                        )
                    ),
                )
            )
        ).all()
        for subscription_id, subscriber_id, nas_device_id, nas_ip in rows:
            evidence = _SessionNas(nas_device_id, (nas_ip or "").strip() or None)
            if subscription_id is not None:
                if subscription_id in by_subscription:
                    by_subscription[subscription_id].add(evidence)
                continue
            for owned in subscriptions_by_account.get(subscriber_id, ()):
                by_subscription[owned].add(evidence)
        for subscription_id, values in by_subscription.items():
            self._sessions[subscription_id] = tuple(
                sorted(
                    values,
                    key=lambda item: (
                        str(item.nas_device_id),
                        item.nas_ip_address or "",
                    ),
                )
            )

    # -- readiness -------------------------------------------------------
    def _router_readiness(
        self, router_ids: Iterable[UUID]
    ) -> Mapping[UUID, WalledGardenRouterReadiness | None]:
        missing = frozenset(
            router_id for router_id in router_ids if router_id not in self._readiness
        )
        if missing:
            resolved = resolve_routers_walled_garden_readiness(
                self.db,
                query=RoutersWalledGardenReadinessQuery(
                    router_ids=missing,
                    max_snapshot_age=self.max_snapshot_age,
                    evaluated_at=self.evaluated_at,
                ),
            )
            for router_id in missing:
                self._readiness[router_id] = resolved.get(router_id)
        return self._readiness

    # -- decision --------------------------------------------------------
    def decide(self, subscription: Subscription) -> CaptiveRouterGateDecision:
        if subscription.id not in self._sessions:
            self._load_sessions([subscription])
        sources: dict[UUID, set[ServingNasSource]] = {}
        causes: set[RouterUnresolvedCause] = set()
        if subscription.provisioning_nas_device_id is not None:
            sources.setdefault(subscription.provisioning_nas_device_id, set()).add(
                ServingNasSource.provisioning_assignment
            )
        ip_index: dict[str, UUID | None] | None = None
        for session in self._sessions.get(subscription.id, ()):
            nas_id = session.nas_device_id
            if nas_id is None and session.nas_ip_address:
                ip_index = ip_index if ip_index is not None else self._nas_ip_index()
                nas_id = ip_index.get(session.nas_ip_address)
            if nas_id is None:
                causes.add(RouterUnresolvedCause.session_nas_unidentified)
                continue
            sources.setdefault(nas_id, set()).add(ServingNasSource.active_session)
        if not sources and not causes:
            causes.add(RouterUnresolvedCause.no_serving_nas)

        router_index = self._router_index()
        serving: list[ServingNasEvidence] = []
        router_rows: dict[UUID, _RouterRow] = {}
        for nas_id in sorted(sources, key=str):
            routers = router_index.get(nas_id, ())
            if not routers:
                causes.add(RouterUnresolvedCause.nas_without_active_router)
            for row in routers:
                router_rows[row.router_id] = row
            serving.append(
                ServingNasEvidence(
                    nas_device_id=nas_id,
                    sources=tuple(sorted(sources[nas_id])),
                    router_ids=tuple(row.router_id for row in routers),
                )
            )

        readiness = self._router_readiness(router_rows)
        routers_out: list[ServingRouterReadiness] = []
        all_ready = True
        for router_id, row in sorted(
            router_rows.items(), key=lambda item: (item[1].name, str(item[0]))
        ):
            result = readiness.get(router_id)
            status = (
                result.status
                if result is not None
                else WalledGardenReadinessStatus.no_snapshot
            )
            all_ready = all_ready and status is WalledGardenReadinessStatus.ready
            routers_out.append(
                ServingRouterReadiness(
                    router_id=router_id,
                    router_name=row.name,
                    status=status,
                    snapshot_captured_at=(
                        result.snapshot_captured_at if result is not None else None
                    ),
                )
            )

        if causes:
            status_out = CaptiveRouterGateStatus.router_unresolved
        elif not all_ready or not routers_out:
            status_out = CaptiveRouterGateStatus.router_not_ready
        else:
            status_out = CaptiveRouterGateStatus.ready
        return CaptiveRouterGateDecision(
            subscription_id=subscription.id,
            status=status_out,
            serving_nas=tuple(serving),
            routers=tuple(routers_out),
            unresolved_causes=tuple(sorted(causes)),
        )


__all__ = [
    "CaptiveRouterGate",
    "CaptiveRouterGateDecision",
    "CaptiveRouterGateStatus",
    "RouterUnresolvedCause",
    "ServingNasEvidence",
    "ServingNasSource",
    "ServingRouterReadiness",
]
