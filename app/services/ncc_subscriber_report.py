"""NCC quarterly Subscriber & Capacity return — subscriber aggregation.

Produces the counts NCC's "Quarterly Subscriber Data Information Request" asks
for, entirely from the subscriber/catalog data: active subscriptions split by
customer type, connection (wired/wireless), billing (prepaid/postpaid), speed
band, State and geopolitical region.

The three network-capacity lines (installed/un-utilised capacity, PoP count,
data-usage TB) are NOT subscriber data — they are supplied as manual inputs by
the caller and merely echoed into the return.

Aggregation runs in Python (not SQL group-by) so it can read the
``Subscriber.category`` JSON-metadata property and stays dialect-independent.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.catalog import AccessType, BillingMode, Subscription, SubscriptionStatus
from app.models.subscriber import Subscriber, SubscriberCategory

# ── Nigerian geography ──────────────────────────────────────────────────────
# Canonical state → geopolitical zone (36 states + FCT). NCC groups per-region
# by these six zones.
_STATE_ZONE: dict[str, str] = {
    # North Central
    "Benue": "North Central",
    "Federal Capital Territory": "North Central",
    "Kogi": "North Central",
    "Kwara": "North Central",
    "Nasarawa": "North Central",
    "Niger": "North Central",
    "Plateau": "North Central",
    # North East
    "Adamawa": "North East",
    "Bauchi": "North East",
    "Borno": "North East",
    "Gombe": "North East",
    "Taraba": "North East",
    "Yobe": "North East",
    # North West
    "Jigawa": "North West",
    "Kaduna": "North West",
    "Kano": "North West",
    "Katsina": "North West",
    "Kebbi": "North West",
    "Sokoto": "North West",
    "Zamfara": "North West",
    # South East
    "Abia": "South East",
    "Anambra": "South East",
    "Ebonyi": "South East",
    "Enugu": "South East",
    "Imo": "South East",
    # South South
    "Akwa Ibom": "South South",
    "Bayelsa": "South South",
    "Cross River": "South South",
    "Delta": "South South",
    "Edo": "South South",
    "Rivers": "South South",
    # South West
    "Ekiti": "South West",
    "Lagos": "South West",
    "Ogun": "South West",
    "Ondo": "South West",
    "Osun": "South West",
    "Oyo": "South West",
}
# Lower-cased lookup for normalisation, plus common aliases for free-text regions.
_STATE_CANON = {s.lower(): s for s in _STATE_ZONE}
_STATE_ALIASES = {
    "abuja": "Federal Capital Territory",
    "fct": "Federal Capital Territory",
    "fct abuja": "Federal Capital Territory",
    "abuja fct": "Federal Capital Territory",
    "akwa-ibom": "Akwa Ibom",
    "cross-river": "Cross River",
    "nassarawa": "Nasarawa",
}
_UNKNOWN = "Unknown"

_WIRED = {AccessType.fiber, AccessType.dsl, AccessType.cable}
_WIRELESS = {AccessType.fixed_wireless}
_CORPORATE = {
    SubscriberCategory.business,
    SubscriberCategory.government,
    SubscriberCategory.ngo,
}


def normalize_state(region: object) -> str:
    """Free-text subscriber region → a canonical Nigerian state name (or Unknown)."""
    if not region:
        return _UNKNOWN
    key = " ".join(str(region).strip().lower().split())
    if key.endswith(" state"):
        key = key[: -len(" state")].strip()
    if key in _STATE_ALIASES:
        return _STATE_ALIASES[key]
    return _STATE_CANON.get(key, _UNKNOWN)


def zone_for_state(state: str) -> str:
    return _STATE_ZONE.get(state, _UNKNOWN)


def speed_band(mbps: int | None) -> str:
    """NCC download-speed bands. Mbps is an integer on the offer, so the 256 kbps
    floor collapses to the sub-2 Mbps band."""
    if not isinstance(mbps, int):
        return "unknown"
    if mbps < 2:
        return "256kbps-<2Mbps"
    if mbps < 10:
        return "2Mbps-<10Mbps"
    return "10Mbps+"


@dataclass(slots=True)
class NccSubscriberReportParams:
    """Pickable parameters for the NCC subscriber return.

    - ``as_of``: the period-end / "as at" date the return is reported against
      (e.g. the last day of the quarter). Subscriptions are counted at that
      point in time. Defaults to now.
    - ``active_statuses``: which subscription statuses count as "active". NCC's
      "active" excludes churned/expired; defaults to ``active`` only, but the
      operator can widen it (e.g. include ``suspended``) per NCC guidance.
    - ``reseller_id``: optionally scope the return to one reseller/partner.
    - ``capacity``: manual network figures (access_capacity_gbps,
      unutilized_capacity_mbps, points_of_presence, data_usage_tb) echoed into
      the return — these are not subscriber data.
    """

    as_of: datetime | None = None
    active_statuses: tuple[SubscriptionStatus, ...] = (SubscriptionStatus.active,)
    reseller_id: uuid.UUID | None = None
    capacity: dict = field(default_factory=dict)


def build_ncc_subscriber_report(
    session: Session,
    params: NccSubscriberReportParams | None = None,
) -> dict:
    """Aggregate subscriptions active at ``params.as_of`` into the NCC quarterly
    subscriber return, per the picked parameters."""
    params = params or NccSubscriberReportParams()
    as_of = params.as_of or datetime.now(UTC)
    statuses = tuple(params.active_statuses) or (SubscriptionStatus.active,)

    query = (
        session.query(Subscription)
        .options(joinedload(Subscription.offer), joinedload(Subscription.subscriber))
        .filter(Subscription.status.in_(statuses))
        # Point-in-time "active as at as_of": started by then, not yet ended or
        # cancelled. (There is no per-subscription last-online column, so status
        # + lifecycle dates are the available signal for the 90-day-active rule.)
        .filter(or_(Subscription.start_at.is_(None), Subscription.start_at <= as_of))
        .filter(or_(Subscription.end_at.is_(None), Subscription.end_at > as_of))
        .filter(
            or_(Subscription.canceled_at.is_(None), Subscription.canceled_at > as_of)
        )
    )
    if params.reseller_id is not None:
        query = query.join(
            Subscriber, Subscription.subscriber_id == Subscriber.id
        ).filter(Subscriber.reseller_id == params.reseller_id)
    subs = query.all()

    total = 0
    connection: Counter = Counter()  # wired / wireless / unknown
    customer_type: Counter = Counter()  # corporate / individual
    billing: Counter = Counter()  # prepaid / postpaid
    bands: Counter = Counter()  # speed bands
    by_state: Counter = Counter()
    by_region: Counter = Counter()
    matrix: Counter = Counter()  # (corporate|individual, wired|wireless)

    for sub in subs:
        offer = sub.offer
        subscriber = sub.subscriber
        total += 1

        conn = "unknown"
        if offer is not None and offer.access_type is not None:
            conn = (
                "wired"
                if offer.access_type in _WIRED
                else "wireless"
                if offer.access_type in _WIRELESS
                else "unknown"
            )
        connection[conn] += 1

        ctype = "individual"
        if subscriber is not None and subscriber.category in _CORPORATE:
            ctype = "corporate"
        customer_type[ctype] += 1
        if conn in ("wired", "wireless"):
            matrix[(ctype, conn)] += 1

        mode = (
            sub.billing_mode
            or (offer.billing_mode if offer else None)
            or BillingMode.prepaid
        )
        billing["postpaid" if mode == BillingMode.postpaid else "prepaid"] += 1

        bands[speed_band(offer.speed_download_mbps if offer else None)] += 1

        state = normalize_state(
            getattr(subscriber, "region", None) if subscriber else None
        )
        by_state[state] += 1
        by_region[zone_for_state(state)] += 1

    cap = params.capacity or {}
    return {
        "parameters": {
            "as_of": as_of.isoformat(),
            "active_statuses": [s.value for s in statuses],
            "reseller_id": str(params.reseller_id) if params.reseller_id else None,
        },
        "as_of": as_of.isoformat(),
        "total_active_subscriptions": total,
        "by_connection": dict(connection),
        "by_customer_type": dict(customer_type),
        "by_billing_mode": dict(billing),
        "by_speed_band": {
            "256kbps-<2Mbps": bands.get("256kbps-<2Mbps", 0),
            "2Mbps-<10Mbps": bands.get("2Mbps-<10Mbps", 0),
            "10Mbps+": bands.get("10Mbps+", 0),
            "unknown": bands.get("unknown", 0),
        },
        # NCC 6a/6b: corporate vs individual, each split wired/wireless.
        "subscription_matrix": {
            "corporate": {
                "wired": matrix.get(("corporate", "wired"), 0),
                "wireless": matrix.get(("corporate", "wireless"), 0),
            },
            "individual": {
                "wired": matrix.get(("individual", "wired"), 0),
                "wireless": matrix.get(("individual", "wireless"), 0),
            },
        },
        "by_state": dict(sorted(by_state.items())),
        "by_region": dict(sorted(by_region.items())),
        # Manual network-capacity inputs, echoed for the return.
        "network_capacity": {
            "access_capacity_gbps": cap.get("access_capacity_gbps"),
            "unutilized_capacity_mbps": cap.get("unutilized_capacity_mbps"),
            "points_of_presence": cap.get("points_of_presence"),
            "data_usage_tb": cap.get("data_usage_tb"),
        },
    }


# ── request-parameter parsing (for the admin picker form / query string) ────
_CAPACITY_KEYS = (
    "access_capacity_gbps",
    "unutilized_capacity_mbps",
    "points_of_presence",
    "data_usage_tb",
)


def parse_report_params(
    *,
    as_of: str | None = None,
    statuses: str | None = None,
    reseller_id: str | None = None,
    capacity: dict[str, str | None] | None = None,
) -> NccSubscriberReportParams:
    """Build ``NccSubscriberReportParams`` from raw form/query strings.

    ``as_of`` is a ``YYYY-MM-DD`` date (interpreted as end-of-day UTC — the
    period-end). ``statuses`` is a comma-separated list of subscription-status
    names; unknown names are ignored, and an empty result falls back to
    ``active``. Bad values degrade to sensible defaults rather than erroring, so
    the picker form is forgiving.
    """
    at = None
    if as_of and as_of.strip():
        try:
            d = datetime.strptime(as_of.strip(), "%Y-%m-%d")
            at = d.replace(hour=23, minute=59, second=59, tzinfo=UTC)
        except ValueError:
            at = None

    picked: list[SubscriptionStatus] = []
    for name in (statuses or "").split(","):
        name = name.strip().lower()
        if not name:
            continue
        try:
            picked.append(SubscriptionStatus(name))
        except ValueError:
            continue
    statuses_tuple = tuple(picked) or (SubscriptionStatus.active,)

    rid = None
    if reseller_id and reseller_id.strip():
        try:
            rid = uuid.UUID(reseller_id.strip())
        except (ValueError, AttributeError):
            rid = None

    cap: dict[str, object] = {}
    for key in _CAPACITY_KEYS:
        raw = (capacity or {}).get(key)
        if raw is not None and str(raw).strip() != "":
            cap[key] = str(raw).strip()

    return NccSubscriberReportParams(
        as_of=at, active_statuses=statuses_tuple, reseller_id=rid, capacity=cap
    )


def build_ncc_subscriber_csv(report: dict) -> str:
    """Flatten a report into the NCC "indicator, value" CSV layout."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["NCC Quarterly Subscriber Data", ""])
    w.writerow(["As at", report["parameters"]["as_of"]])
    w.writerow(["Active statuses", ", ".join(report["parameters"]["active_statuses"])])
    w.writerow([])
    w.writerow(["Indicator", "Value"])
    w.writerow(
        ["Total Active Internet Subscriptions", report["total_active_subscriptions"]]
    )

    m = report["subscription_matrix"]
    w.writerow(["Corporate — Wired", m["corporate"]["wired"]])
    w.writerow(["Corporate — Wireless", m["corporate"]["wireless"]])
    w.writerow(["Individual — Wired", m["individual"]["wired"]])
    w.writerow(["Individual — Wireless", m["individual"]["wireless"]])

    b = report["by_billing_mode"]
    w.writerow(["Prepaid subscribers", b.get("prepaid", 0)])
    w.writerow(["Postpaid subscribers", b.get("postpaid", 0)])

    s = report["by_speed_band"]
    w.writerow(["Speed 256kbps–<2Mbps", s["256kbps-<2Mbps"]])
    w.writerow(["Speed 2Mbps–<10Mbps", s["2Mbps-<10Mbps"]])
    w.writerow(["Speed 10Mbps & above", s["10Mbps+"]])

    cap = report["network_capacity"]
    w.writerow(["Access Capacity (Gbps) [manual]", cap["access_capacity_gbps"] or ""])
    w.writerow(
        ["Un-utilised Capacity (Mbps) [manual]", cap["unutilized_capacity_mbps"] or ""]
    )
    w.writerow(
        ["Number of Points of Presence [manual]", cap["points_of_presence"] or ""]
    )
    w.writerow(["Data Usage (TB) [manual]", cap["data_usage_tb"] or ""])

    w.writerow([])
    w.writerow(["Active Subscriptions per State", ""])
    for state, n in report["by_state"].items():
        w.writerow([state, n])
    w.writerow([])
    w.writerow(["Active Subscriptions per Region", ""])
    for region, n in report["by_region"].items():
        w.writerow([region, n])
    return buf.getvalue()
