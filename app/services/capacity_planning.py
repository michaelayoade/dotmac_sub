"""What has been sold behind a shared segment, against what it can carry.

Owner of "does this segment have room". Contention was an integer on a
catalogue offer that nothing enforced and nothing measured; this compares the
sold total against the recorded capacity of the segment it is actually sold
behind.

Two rules shape everything here.

**Committed capacity is not the same as sold capacity.** A best-effort tier
sells a ceiling; a dedicated tier with a CIR *reserves* its rate. Reserved
bandwidth is unavailable to anyone else whether or not it is in use, so it is
counted at full weight, while best-effort peaks are counted against the
oversubscription allowance. Treating them alike would either block every fibre
sale or wave through an unhonourable guarantee.

**A segment with no recorded capacity returns UNKNOWN, never OK.** The check
exists to stop overselling; a missing capacity figure is exactly the state in
which overselling is most likely, so it must not read as a pass.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.capacity import CapacityDomain, CapacityDomainKind
from app.models.catalog import (
    CatalogOffer,
    GuaranteedSpeedType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import OntAssignment
from app.services.domain_errors import DomainError


class CapacityError(DomainError):
    """Unusable capacity input (adapter: HTTP 400)."""


class CapacityVerdict(enum.StrEnum):
    """Whether a segment can take more.

    ``unknown`` is never silently treated as ``ok`` — an unmeasured segment is
    where overselling hides.
    """

    ok = "ok"
    at_risk = "at_risk"
    oversubscribed = "oversubscribed"
    #: Committed rates alone exceed physical capacity. Distinct from
    #: oversubscribed because no amount of statistical multiplexing helps: the
    #: promises cannot all be kept simultaneously.
    overcommitted = "overcommitted"
    unknown = "unknown"


#: Sold above this share of the allowance flags a segment before it breaches.
_AT_RISK_SHARE = Decimal("0.85")


@dataclass(frozen=True, slots=True)
class CapacityUsage:
    """One segment's position. Immutable: it is evidence for a sales decision."""

    domain_id: object
    domain_name: str
    kind: CapacityDomainKind
    #: None until the segment has been surveyed — an unmeasured segment, not a
    #: segment with no capacity.
    downstream_mbps: int | None
    upstream_mbps: int | None
    target_oversubscription: Decimal
    subscriber_count: int
    #: Sum of peak (ceiling) rates sold behind this segment.
    sold_downstream_mbps: int
    sold_upstream_mbps: int
    #: Sum of rates that are RESERVED, not merely allowed.
    committed_downstream_mbps: int
    committed_upstream_mbps: int
    verdict: CapacityVerdict

    @property
    def sellable_downstream_mbps(self) -> Decimal | None:
        """Capacity times the allowance — the budget sold peaks draw on.

        ``None`` on an unsurveyed segment. Returning 0 would read as "no room"
        and returning a guess would read as room that may not exist; neither is
        the truth, which is that nobody has measured it.
        """
        if not self.downstream_mbps:
            return None
        return Decimal(self.downstream_mbps) * self.target_oversubscription

    @property
    def headroom_downstream_mbps(self) -> Decimal | None:
        sellable = self.sellable_downstream_mbps
        if sellable is None:
            return None
        return sellable - Decimal(self.sold_downstream_mbps)

    @property
    def committed_share(self) -> Decimal | None:
        """Reserved share of physical capacity. Above 1 nothing can save it."""
        if not self.downstream_mbps:
            return None
        return Decimal(self.committed_downstream_mbps) / Decimal(self.downstream_mbps)


def _committed_for(offer: CatalogOffer) -> tuple[int, int]:
    """(upstream, downstream) Mbps this offer RESERVES, not merely allows."""
    if offer.guaranteed_speed is GuaranteedSpeedType.fixed:
        floor = offer.guaranteed_speed_limit_at
        if not floor:
            return 0, 0
        return (
            min(int(floor), offer.speed_upload_mbps or 0),
            min(int(floor), offer.speed_download_mbps or 0),
        )
    if offer.guaranteed_speed is GuaranteedSpeedType.relative:
        percent = offer.guaranteed_speed_limit_at
        if not percent:
            return 0, 0
        percent = max(0, min(int(percent), 100))
        return (
            (offer.speed_upload_mbps or 0) * percent // 100,
            (offer.speed_download_mbps or 0) * percent // 100,
        )
    return 0, 0


def _verdict(
    *,
    downstream_mbps: int | None,
    sold_downstream_mbps: int,
    committed_downstream_mbps: int,
    target_oversubscription: Decimal,
) -> CapacityVerdict:
    # An unsurveyed segment cannot be judged. Returning ok here would make the
    # 502 ports nobody has measured look like the safest on the network.
    if not downstream_mbps:
        return CapacityVerdict.unknown
    # Checked before oversubscription: committed rates exceeding physical
    # capacity cannot be rescued by an allowance, because they are reserved
    # whether or not anyone is transmitting.
    if committed_downstream_mbps > downstream_mbps:
        return CapacityVerdict.overcommitted
    sellable = Decimal(downstream_mbps) * target_oversubscription
    if not sellable:
        return CapacityVerdict.unknown
    sold = Decimal(sold_downstream_mbps)
    if sold > sellable:
        return CapacityVerdict.oversubscribed
    if sold >= sellable * _AT_RISK_SHARE:
        return CapacityVerdict.at_risk
    return CapacityVerdict.ok


def _pon_subscription_offers(db: Session) -> dict[object, list[CatalogOffer]]:
    """PON port id -> the offers of the active subscriptions behind it.

    The path is ONT assignment -> subscriber -> active subscription, which is
    the only populated route from a customer to a shared segment today.
    """
    rows = (
        db.query(OntAssignment.pon_port_id, CatalogOffer)
        .join(Subscription, Subscription.subscriber_id == OntAssignment.subscriber_id)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .filter(
            OntAssignment.pon_port_id.isnot(None),
            OntAssignment.active.is_(True),
            Subscription.status == SubscriptionStatus.active,
        )
        .all()
    )
    grouped: dict[object, list[CatalogOffer]] = {}
    for pon_port_id, offer in rows:
        grouped.setdefault(pon_port_id, []).append(offer)
    return grouped


def usage_for_domains(db: Session) -> list[CapacityUsage]:
    """Every active capacity domain with what is sold behind it.

    Only PON ports resolve to subscribers today — that is the one populated
    path. Wireless sectors, OLT uplinks and BNGs return zero sold with an
    ``unknown`` verdict rather than a reassuring ``ok``, because "no data" and
    "no load" must not look the same to whoever reads this.
    """
    by_pon = _pon_subscription_offers(db)
    usages: list[CapacityUsage] = []

    domains = (
        db.query(CapacityDomain)
        .filter(CapacityDomain.is_active.is_(True))
        .order_by(CapacityDomain.name)
        .all()
    )
    for domain in domains:
        offers: list[CatalogOffer] = []
        resolvable = domain.kind is CapacityDomainKind.pon_port
        if resolvable:
            offers = by_pon.get(domain.pon_port_id, [])

        sold_down = sum(offer.speed_download_mbps or 0 for offer in offers)
        sold_up = sum(offer.speed_upload_mbps or 0 for offer in offers)
        committed = [_committed_for(offer) for offer in offers]
        committed_up = sum(item[0] for item in committed)
        committed_down = sum(item[1] for item in committed)
        target = Decimal(str(domain.target_oversubscription))

        verdict = (
            _verdict(
                downstream_mbps=domain.downstream_mbps,
                sold_downstream_mbps=sold_down,
                committed_downstream_mbps=committed_down,
                target_oversubscription=target,
            )
            if resolvable
            else CapacityVerdict.unknown
        )

        usages.append(
            CapacityUsage(
                domain_id=domain.id,
                domain_name=domain.name,
                kind=domain.kind,
                downstream_mbps=domain.downstream_mbps,
                upstream_mbps=domain.upstream_mbps,
                target_oversubscription=target,
                subscriber_count=len(offers),
                sold_downstream_mbps=sold_down,
                sold_upstream_mbps=sold_up,
                committed_downstream_mbps=committed_down,
                committed_upstream_mbps=committed_up,
                verdict=verdict,
            )
        )
    return usages


def can_accept(
    usage: CapacityUsage, offer: CatalogOffer
) -> tuple[bool, CapacityVerdict, str]:
    """Would adding ``offer`` to this segment still be within budget?

    Returns the verdict the segment WOULD have, so a service order can record
    the finding rather than only a yes/no. An unknown segment refuses: the
    order should carry "capacity not established", not a silent pass.
    """
    if usage.verdict is CapacityVerdict.unknown:
        return False, CapacityVerdict.unknown, "segment capacity is not established"

    committed_up, committed_down = _committed_for(offer)
    projected = _verdict(
        downstream_mbps=usage.downstream_mbps,
        sold_downstream_mbps=usage.sold_downstream_mbps
        + (offer.speed_download_mbps or 0),
        committed_downstream_mbps=usage.committed_downstream_mbps + committed_down,
        target_oversubscription=usage.target_oversubscription,
    )
    if projected is CapacityVerdict.overcommitted:
        return (
            False,
            projected,
            "reserved rates would exceed the segment's physical capacity",
        )
    if projected is CapacityVerdict.oversubscribed:
        return False, projected, "sold capacity would exceed the planning target"
    return True, projected, "within the planning target"
