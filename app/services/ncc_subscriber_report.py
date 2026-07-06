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

import re
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
    "f c t": "Federal Capital Territory",
    "fct abuja": "Federal Capital Territory",
    "abuja fct": "Federal Capital Territory",
    "federal capital territory abuja": "Federal Capital Territory",
    "abuja federal capital territory": "Federal Capital Territory",
    "akwa-ibom": "Akwa Ibom",
    "cross-river": "Cross River",
    "nassarawa": "Nasarawa",
}

# City / area names that unambiguously identify a Nigerian state for NCC state
# aggregation. This is report-only normalization; stored customer profiles are
# not modified.
_PLACE_STATE_ALIASES = {
    # FCT / Abuja districts
    "abuja": "Federal Capital Territory",
    "area 1": "Federal Capital Territory",
    "area 2": "Federal Capital Territory",
    "area 3": "Federal Capital Territory",
    "area 7": "Federal Capital Territory",
    "area 8": "Federal Capital Territory",
    "area 10": "Federal Capital Territory",
    "area 11": "Federal Capital Territory",
    "central business district": "Federal Capital Territory",
    "cbd": "Federal Capital Territory",
    "dakwo": "Federal Capital Territory",
    "dawaki": "Federal Capital Territory",
    "apo": "Federal Capital Territory",
    "asokoro": "Federal Capital Territory",
    "f c t": "Federal Capital Territory",
    "fct": "Federal Capital Territory",
    "fct abuja": "Federal Capital Territory",
    "garki": "Federal Capital Territory",
    "gudu": "Federal Capital Territory",
    "gaduwa": "Federal Capital Territory",
    "guzape": "Federal Capital Territory",
    "gwarinpa": "Federal Capital Territory",
    "gwarimpa": "Federal Capital Territory",
    "idu": "Federal Capital Territory",
    "jabi": "Federal Capital Territory",
    "jahi": "Federal Capital Territory",
    "kado": "Federal Capital Territory",
    "karu abuja": "Federal Capital Territory",
    "karsana": "Federal Capital Territory",
    "kubwa": "Federal Capital Territory",
    "katampe": "Federal Capital Territory",
    "kantape": "Federal Capital Territory",
    "life camp": "Federal Capital Territory",
    "lokogoma": "Federal Capital Territory",
    "lugbe": "Federal Capital Territory",
    "maitama": "Federal Capital Territory",
    "sun city abuja": "Federal Capital Territory",
    "suncity abuja": "Federal Capital Territory",
    "utako": "Federal Capital Territory",
    "wuse": "Federal Capital Territory",
    "wuye": "Federal Capital Territory",
    # Lagos city / districts
    "ajah": "Lagos",
    "abule egba": "Lagos",
    "ayobo": "Lagos",
    "festac": "Lagos",
    "ebute metta": "Lagos",
    "ebutte metta": "Lagos",
    "ikeja": "Lagos",
    "ikorodu": "Lagos",
    "ipaja": "Lagos",
    "lagos city": "Lagos",
    "lekki": "Lagos",
    "ogudu": "Lagos",
    "oshodi": "Lagos",
    "oworonshoki": "Lagos",
    "kosofe": "Lagos",
    "surulere": "Lagos",
    "victoria island": "Lagos",
    "vi": "Lagos",
    "yaba": "Lagos",
    # Other common city aliases
    "port harcourt": "Rivers",
    "ph": "Rivers",
    "phc": "Rivers",
    "ibadan": "Oyo",
    "awka": "Anambra",
}
_UNKNOWN = "Unknown"

_WIRED = {AccessType.fiber, AccessType.dsl, AccessType.cable}
_WIRELESS = {AccessType.fixed_wireless}
_CORPORATE = {
    SubscriberCategory.business,
    SubscriberCategory.government,
    SubscriberCategory.ngo,
}
_MAX_PLAUSIBLE_SPEED_MBPS = 10_000


def normalize_state(region: object) -> str:
    """Free-text subscriber region → a canonical Nigerian state name (or Unknown)."""
    key = _normalize_location_key(region)
    if not key:
        return _UNKNOWN
    if key.endswith(" state"):
        key = key[: -len(" state")].strip()
    if key in _STATE_ALIASES:
        return _STATE_ALIASES[key]
    return _STATE_CANON.get(key, _UNKNOWN)


def zone_for_state(state: str) -> str:
    return _STATE_ZONE.get(state, _UNKNOWN)


def _normalize_location_key(value: object) -> str:
    if value is None:
        return ""
    raw = str(value).strip().lower()
    if not raw or raw in {"none", "null", "n/a", "na", "unknown"}:
        return ""
    raw = raw.replace("&", " and ")
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return " ".join(raw.split())


def _state_from_place(value: object) -> str:
    key = _normalize_location_key(value)
    if not key:
        return _UNKNOWN
    state = normalize_state(key)
    if state != _UNKNOWN:
        return state
    return _PLACE_STATE_ALIASES.get(key, _UNKNOWN)


def _state_from_address_text(value: object) -> str:
    key = _normalize_location_key(value)
    if not key:
        return _UNKNOWN
    state = normalize_state(key)
    if state != _UNKNOWN:
        return state
    # Phrase containment is only used for unambiguous city/district aliases.
    padded = f" {key} "
    for alias, alias_state in _PLACE_STATE_ALIASES.items():
        if f" {alias} " in padded:
            return alias_state
    return _UNKNOWN


def _metadata_location_values(metadata: dict | None) -> list[object]:
    if not isinstance(metadata, dict):
        return []
    values: list[object] = []
    for key in (
        "state",
        "region",
        "city",
        "location",
        "service_location",
        "installation_address",
        "service_address",
        "address",
    ):
        value = metadata.get(key)
        if isinstance(value, dict):
            values.extend(
                value.get(n) for n in ("state", "region", "city", "location", "address")
            )
        else:
            values.append(value)
    return values


def infer_state(subscriber: Subscriber | None) -> str:
    """Resolve a subscriber to a canonical state for the NCC aggregate only."""
    if subscriber is None:
        return _UNKNOWN

    # State-like fields first. If present and valid, they are the strongest
    # signal and avoid converting display values such as "Lekki" into profile
    # data; this is only the report's canonical state projection.
    for value in (
        getattr(subscriber, "region", None),
        getattr(subscriber, "billing_region", None),
    ):
        state = normalize_state(value)
        if state != _UNKNOWN:
            return state

    addresses = list(getattr(subscriber, "addresses", None) or [])
    addresses.sort(
        key=lambda a: (
            0 if getattr(a, "is_primary", False) else 1,
            0 if str(getattr(a, "address_type", "")).endswith("service") else 1,
        )
    )
    for address in addresses:
        for value in (getattr(address, "region", None),):
            state = normalize_state(value)
            if state != _UNKNOWN:
                return state

    # City / location-like fields. These use a conservative alias table for
    # cities and districts that unambiguously identify a Nigerian state.
    for value in (
        getattr(subscriber, "city", None),
        getattr(subscriber, "billing_city", None),
    ):
        state = _state_from_place(value)
        if state != _UNKNOWN:
            return state
    for address in addresses:
        state = _state_from_place(getattr(address, "city", None))
        if state != _UNKNOWN:
            return state

    # Address text and selected metadata keys are weakest signals; they are only
    # searched for explicit state names or unambiguous aliases.
    for value in (
        getattr(subscriber, "address_line1", None),
        getattr(subscriber, "address_line2", None),
        getattr(subscriber, "billing_address_line1", None),
        getattr(subscriber, "billing_address_line2", None),
    ):
        state = _state_from_address_text(value)
        if state != _UNKNOWN:
            return state
    for address in addresses:
        for value in (
            getattr(address, "address_line1", None),
            getattr(address, "address_line2", None),
        ):
            state = _state_from_address_text(value)
            if state != _UNKNOWN:
                return state
    for value in _metadata_location_values(getattr(subscriber, "metadata_", None)):
        state = _state_from_address_text(value)
        if state != _UNKNOWN:
            return state
    return _UNKNOWN


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


def _plausible_speed_mbps(value: object) -> int | None:
    """Return a speed only when it is a plausible Mbps catalogue value."""
    if not isinstance(value, int):
        return None
    if value <= 0 or value > _MAX_PLAUSIBLE_SPEED_MBPS:
        return None
    return value


def _average_speed_payload(
    *,
    total: int,
    download_values: list[int],
    upload_values: list[int],
    excluded_download_count: int,
    excluded_upload_count: int,
) -> dict[str, object]:
    def avg(values: list[int]) -> float | None:
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    return {
        # NCC asks for average internet speed; download is the defensible single
        # speed figure because the existing NCC bands are download-speed bands.
        "average_mbps": avg(download_values),
        "average_download_mbps": avg(download_values),
        "average_upload_mbps": avg(upload_values),
        "basis": "active_subscription_offer_speed_mbps",
        "included_download_count": len(download_values),
        "included_upload_count": len(upload_values),
        "excluded_download_count": excluded_download_count,
        "excluded_upload_count": excluded_upload_count,
        "total_active_subscriptions": total,
        "max_plausible_speed_mbps": _MAX_PLAUSIBLE_SPEED_MBPS,
    }


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
        .options(
            joinedload(Subscription.offer),
            joinedload(Subscription.subscriber).joinedload(Subscriber.addresses),
        )
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
    download_speeds: list[int] = []
    upload_speeds: list[int] = []
    excluded_download_speed_count = 0
    excluded_upload_speed_count = 0

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
        download_speed = _plausible_speed_mbps(
            offer.speed_download_mbps if offer else None
        )
        if download_speed is None:
            excluded_download_speed_count += 1
        else:
            download_speeds.append(download_speed)
        upload_speed = _plausible_speed_mbps(offer.speed_upload_mbps if offer else None)
        if upload_speed is None:
            excluded_upload_speed_count += 1
        else:
            upload_speeds.append(upload_speed)

        state = infer_state(subscriber)
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
        "average_speed": _average_speed_payload(
            total=total,
            download_values=download_speeds,
            upload_values=upload_speeds,
            excluded_download_count=excluded_download_speed_count,
            excluded_upload_count=excluded_upload_speed_count,
        ),
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
    avg = report["average_speed"]
    w.writerow(["Average Internet Speed (Mbps)", avg["average_mbps"] or ""])
    w.writerow(
        [
            "Average Download Speed (Mbps)",
            avg["average_download_mbps"] or "",
        ]
    )
    w.writerow(["Average Upload Speed (Mbps)", avg["average_upload_mbps"] or ""])
    w.writerow(
        [
            "Average Speed Included Subscriptions",
            avg["included_download_count"],
        ]
    )
    w.writerow(
        [
            "Average Speed Excluded Subscriptions",
            avg["excluded_download_count"],
        ]
    )

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
