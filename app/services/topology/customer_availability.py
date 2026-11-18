"""Per-customer service availability for a period (agent-facing).

Answers "what availability did this customer actually get?" when they complain
about downtime, using the operator's own measured data rather than an argument.

**Availability is infrastructure-based, not device-based.** A customer's own
ONT/router being off — power cut, unplugged, CPE fault — is not a provider
availability failure, so RADIUS session gaps and ONT-offline time are
deliberately NOT counted. The base number is the availability of the shared
infrastructure serving them (BTS/POP site, OLT, PON port, upstream nodes), which
``AvailabilitySnapshot`` already measures per element per day.

**The exception is an individual provider-fault ticket.** A last-mile fault the
provider owns and was told about — fibre cut to the premises, realignment,
customer link disconnection — is downtime the customer is entitled to count,
evidenced by a support ticket. Its open→resolved span inside the period is added.

Overlapping infrastructure is combined as *worst-per-day*, not summed: if the
OLT and the POP site are both down in the same window the customer is down once,
not twice.

Nothing here decides outage truth or path — ``topology.customer_path`` and the
availability snapshots own that. This composes them for one customer, read-only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.models.network_monitoring import AvailabilitySnapshot

logger = logging.getLogger(__name__)

#: AvailabilitySnapshot.element_type values we can attribute to a customer.
ELEMENT_DEVICE = "device"
ELEMENT_POP_SITE = "pop_site"
ELEMENT_PON_PORT = "pon_port"


@dataclass
class ServingElement:
    """One infrastructure element whose availability this customer depends on."""

    element_type: str
    element_id: uuid.UUID
    label: str
    role: str
    downtime_seconds: int = 0

    @property
    def trend_url(self) -> str:
        return (
            "/admin/network/performance/trend"
            f"?element_type={self.element_type}&element_id={self.element_id}"
        )


@dataclass
class ProviderFaultTicket:
    """An individual, provider-attributable fault counted against availability."""

    ticket_id: uuid.UUID
    number: str | None
    title: str
    ticket_type: str | None
    opened_at: datetime
    resolved_at: datetime | None
    downtime_seconds: int

    @property
    def url(self) -> str:
        return f"/admin/support/tickets/{self.ticket_id}"


@dataclass
class CustomerAvailability:
    """One customer's availability for a period, with its evidence."""

    period_days: int
    period_start: datetime
    period_end: datetime
    period_seconds: int
    serving_elements: list[ServingElement] = field(default_factory=list)
    infrastructure_downtime_seconds: int = 0
    infrastructure_observed_days: int = 0
    tickets: list[ProviderFaultTicket] = field(default_factory=list)
    ticket_downtime_seconds: int = 0
    path_gap: str | None = None

    @property
    def has_infrastructure_coverage(self) -> bool:
        """Whether every serving element has trustworthy evidence every day.

        Anything less is partial monitoring, so missing days or path elements
        must not be silently counted as uptime.
        """

        return (
            bool(self.serving_elements)
            and self.period_days > 0
            and self.infrastructure_observed_days >= self.period_days
        )

    @property
    def infrastructure_uptime_percent(self) -> float:
        return self._percent(self.infrastructure_downtime_seconds)

    @property
    def effective_downtime_seconds(self) -> int:
        return self.infrastructure_downtime_seconds + self.ticket_downtime_seconds

    @property
    def effective_uptime_percent(self) -> float:
        return self._percent(self.effective_downtime_seconds)

    def _percent(self, downtime: int) -> float:
        if self.period_seconds <= 0:
            return 100.0
        pct = 100.0 * (1.0 - (downtime / self.period_seconds))
        return round(max(0.0, min(100.0, pct)), 3)


def _serving_elements(
    session: Session, subscription
) -> tuple[list[ServingElement], str | None]:
    """Resolve the infrastructure this customer sits behind."""
    from app.services.topology.customer_path import resolve_customer_path

    path = resolve_customer_path(session, subscription)
    elements: list[ServingElement] = []
    seen: set[tuple[str, uuid.UUID]] = set()

    def _add(element_type: str, obj, role: str) -> None:
        if obj is None:
            return
        element_id = getattr(obj, "id", None)
        if element_id is None or (element_type, element_id) in seen:
            return
        seen.add((element_type, element_id))
        label = (
            getattr(obj, "name", None)
            or getattr(obj, "hostname", None)
            or getattr(obj, "label", None)
            or str(element_id)[:8]
        )
        elements.append(
            ServingElement(
                element_type=element_type,
                element_id=element_id,
                label=str(label),
                role=role,
            )
        )

    _add(ELEMENT_POP_SITE, path.basestation, "Base station")
    _add(ELEMENT_PON_PORT, path.pon_port, "PON port")
    # Snapshot device IDs are NetworkDevice IDs. A raw OLTDevice ID is a
    # different namespace; the matching access node is path.node below.
    if path.access_device_kind == "ap":
        _add(ELEMENT_DEVICE, path.access_device, "Access ap")
    _add(ELEMENT_DEVICE, path.node, "Access node")
    for hop in path.upstream_chain:
        _add(ELEMENT_DEVICE, hop, "Upstream")

    return elements, path.gap


def _infrastructure_downtime(
    session: Session,
    elements: list[ServingElement],
    *,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """Worst-per-day downtime across the serving elements, summed.

    Snapshots are daily aggregates, so per-day overlap between two elements
    cannot be resolved exactly; taking the worst element per day is the honest
    combination — a shared outage counts once, not once per element.
    """
    if not elements:
        return 0, 0

    keys = [(e.element_type, e.element_id) for e in elements]
    rows = (
        session.query(AvailabilitySnapshot)
        .filter(AvailabilitySnapshot.snapshot_date >= start)
        .filter(AvailabilitySnapshot.snapshot_date < end)
        .filter(
            tuple_(
                AvailabilitySnapshot.element_type,
                AvailabilitySnapshot.element_id,
            ).in_(keys)
        )
        .all()
    )
    wanted = set(keys)
    by_day: dict[object, int] = {}
    observed_keys_by_day: dict[object, set[tuple[str, uuid.UUID]]] = {}
    per_element: dict[tuple[str, uuid.UUID], int] = {}

    for row in rows:
        key = (row.element_type, row.element_id)
        if key not in wanted:
            continue
        if row.uptime_percent is None:
            continue
        downtime = int(row.downtime_seconds or 0)
        day = (
            row.snapshot_date.date()
            if hasattr(row.snapshot_date, "date")
            else row.snapshot_date
        )
        by_day[day] = max(by_day.get(day, 0), downtime)
        observed_keys_by_day.setdefault(day, set()).add(key)
        per_element[key] = per_element.get(key, 0) + downtime

    for element in elements:
        element.downtime_seconds = per_element.get(
            (element.element_type, element.element_id), 0
        )

    fully_observed_days = sum(
        1 for observed_keys in observed_keys_by_day.values() if wanted <= observed_keys
    )
    return sum(by_day.values()), fully_observed_days


def _provider_fault_tickets(
    session: Session,
    subscriber_id,
    *,
    start: datetime,
    end: datetime,
) -> tuple[list[ProviderFaultTicket], int]:
    """Individual provider-attributable faults overlapping the period."""
    from app.models.support import Ticket
    from app.services.customer_service_state import is_infrastructure_down_ticket

    if subscriber_id is None:
        return [], 0

    rows = (
        session.query(Ticket)
        .filter(Ticket.subscriber_id == subscriber_id)
        .filter(Ticket.created_at <= end)
        .all()
    )

    tickets: list[ProviderFaultTicket] = []
    total = 0
    for row in rows:
        if not is_infrastructure_down_ticket(row):
            continue
        opened = _as_utc(row.created_at)
        if opened is None:
            continue
        closed = _as_utc(row.resolved_at or row.closed_at) or end
        if closed <= start:
            continue
        span_start = max(opened, start)
        span_end = min(closed, end)
        seconds = max(0, int((span_end - span_start).total_seconds()))
        if seconds <= 0:
            continue
        tickets.append(
            ProviderFaultTicket(
                ticket_id=row.id,
                number=row.number,
                title=row.title,
                ticket_type=row.ticket_type,
                opened_at=opened,
                resolved_at=_as_utc(row.resolved_at or row.closed_at),
                downtime_seconds=seconds,
            )
        )
        total += seconds

    tickets.sort(key=lambda t: t.opened_at, reverse=True)
    return tickets, total


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def customer_availability(
    session: Session,
    subscription,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> CustomerAvailability:
    """Availability this customer received over the trailing ``days``.

    Read-only and non-raising at the path layer: an unresolvable topology path
    yields a report with no infrastructure coverage rather than an error, so the
    agent still sees the ticket evidence.
    """
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)

    try:
        elements, gap = _serving_elements(session, subscription)
    except Exception:
        logger.exception(
            "Customer availability: path resolution failed for subscription %s",
            getattr(subscription, "id", None),
        )
        elements, gap = [], "path resolution failed"

    infra_downtime, observed_days = _infrastructure_downtime(
        session, elements, start=start, end=end
    )
    tickets, ticket_downtime = _provider_fault_tickets(
        session,
        getattr(subscription, "subscriber_id", None),
        start=start,
        end=end,
    )

    return CustomerAvailability(
        period_days=days,
        period_start=start,
        period_end=end,
        period_seconds=int((end - start).total_seconds()),
        serving_elements=elements,
        infrastructure_downtime_seconds=infra_downtime,
        infrastructure_observed_days=observed_days,
        tickets=tickets,
        ticket_downtime_seconds=ticket_downtime,
        path_gap=gap,
    )


class AvailabilityState(StrEnum):
    confirmed = "confirmed"
    estimated = "estimated"
    partial = "partial"
    unavailable = "unavailable"


@dataclass(frozen=True, slots=True)
class CustomerAvailabilityQuery:
    customer_id: uuid.UUID
    period_start: datetime
    period_end: datetime  # exclusive
    subscription_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class CustomerAvailabilityResult:
    state: AvailabilityState
    expected_days: int
    observed_days: int
    period_seconds: float
    downtime_seconds: float | None
    subscription_id: uuid.UUID | None
    subscription_status: str | None
    reason: str | None
    evidence_sources: tuple[str, ...]

    @property
    def downtime_days(self) -> float | None:
        return (
            round(self.downtime_seconds / 86400, 2)
            if self.downtime_seconds is not None
            else None
        )

    @property
    def uptime_days(self) -> float | None:
        if self.downtime_days is None:
            return None
        return round(self.period_seconds / 86400, 2) - self.downtime_days


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    """Return the wall-clock union of clipped ticket intervals."""
    total = 0.0
    latest: datetime | None = None
    for start, end in sorted(intervals):
        if latest is None or start >= latest:
            total += (end - start).total_seconds()
            latest = end
        elif end > latest:
            total += (end - latest).total_seconds()
            latest = end
    return total


def query_customer_availability(
    session: Session, query: CustomerAvailabilityQuery
) -> CustomerAvailabilityResult:
    """Read provider-side evidence for the admin Statistics cards.

    Daily snapshots are the materialized uptime-alert evidence for devices.
    A missing snapshot is unknown monitoring, never positive uptime evidence.
    """
    from app.models.catalog import Subscription
    from app.models.support import Ticket, TicketStatus
    from app.services.customer_service_state import is_infrastructure_down_ticket

    start = _as_utc(query.period_start)
    end = _as_utc(query.period_end)
    if start is None or end is None or end <= start:
        raise ValueError("Availability period must have a positive duration")
    period_seconds = (end - start).total_seconds()
    expected_days = ((end - timedelta(microseconds=1)).date() - start.date()).days + 1

    def unavailable(
        reason: str,
        *,
        state: AvailabilityState = AvailabilityState.unavailable,
        observed: int = 0,
        subscription: Subscription | None = None,
    ) -> CustomerAvailabilityResult:
        return CustomerAvailabilityResult(
            state=state,
            expected_days=expected_days,
            observed_days=observed,
            period_seconds=period_seconds,
            downtime_seconds=None,
            subscription_id=subscription.id if subscription else None,
            subscription_status=(subscription.status.value if subscription else None),
            reason=reason,
            evidence_sources=("customer_path", "availability_snapshots"),
        )

    subscriptions = (
        session.query(Subscription)
        .filter(Subscription.subscriber_id == query.customer_id)
        .all()
    )
    if query.subscription_id is not None:
        subscriptions = [
            sub for sub in subscriptions if sub.id == query.subscription_id
        ]
    covering: list[Subscription] = []
    for sub in subscriptions:
        activated = _as_utc(sub.start_at or sub.created_at)
        ended = _as_utc(sub.canceled_at or sub.end_at)
        if activated is not None and activated <= start and (ended or end) >= end:
            covering.append(sub)
    if len(covering) != 1:
        return unavailable(
            "No single subscription covers this period."
            if not covering
            else "Multiple subscriptions cover this period; availability is service-specific."
        )
    subscription = covering[0]
    if subscription.status.value == "canceled" and subscription.canceled_at is None:
        return unavailable(
            "The canceled subscription has no dated closure evidence.",
            subscription=subscription,
        )
    try:
        elements, gap = _serving_elements(session, subscription)
    except Exception:
        logger.exception("Customer Statistics availability path resolution failed")
        return unavailable(
            "The service network path could not be resolved.", subscription=subscription
        )
    if not elements or gap:
        return unavailable(
            "The service network path is incomplete.", subscription=subscription
        )

    keys = {(element.element_type, element.element_id) for element in elements}
    rows = (
        session.query(AvailabilitySnapshot)
        .filter(
            AvailabilitySnapshot.snapshot_date
            >= start.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        .filter(AvailabilitySnapshot.snapshot_date < end)
        .filter(
            tuple_(
                AvailabilitySnapshot.element_type, AvailabilitySnapshot.element_id
            ).in_(keys)
        )
        .all()
    )
    by_day: dict[date, dict[tuple[str, uuid.UUID], float]] = {}
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor < end:
        next_day = cursor + timedelta(days=1)
        covered = max(0.0, (min(next_day, end) - max(cursor, start)).total_seconds())
        if covered:
            by_day[cursor.date()] = {}
        cursor = next_day
    for row in rows:
        key = (row.element_type, row.element_id)
        day = (_as_utc(row.snapshot_date) or row.snapshot_date).date()
        if key not in keys or day not in by_day:
            continue
        pct = row.uptime_percent
        if pct is None or not 0 <= pct <= 100 or row.window_seconds < 86400:
            continue
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        covered = (
            min(day_start + timedelta(days=1), end) - max(day_start, start)
        ).total_seconds()
        by_day[day][key] = covered * (100 - pct) / 100

    observed = sum(1 for values in by_day.values() if keys <= values.keys())
    if observed != len(by_day):
        return unavailable(
            f"Monitoring data exists for only {observed} of {expected_days} days.",
            state=AvailabilityState.partial
            if observed
            else AvailabilityState.unavailable,
            observed=observed,
            subscription=subscription,
        )

    ticket_rows = (
        session.query(Ticket)
        .filter(Ticket.subscriber_id == query.customer_id)
        .filter(Ticket.created_at < end)
        .all()
    )
    ticket_intervals: list[tuple[datetime, datetime]] = []
    for ticket in ticket_rows:
        if not is_infrastructure_down_ticket(ticket):
            continue
        closed = _as_utc(ticket.resolved_at or ticket.closed_at)
        if ticket.status == TicketStatus.canceled.value and closed is None:
            continue
        opened = _as_utc(ticket.created_at)
        if opened is None or (closed is not None and closed <= opened):
            continue
        interval = (max(opened, start), min(closed or end, end))
        if interval[1] > interval[0]:
            ticket_intervals.append(interval)

    downtime = 0.0
    for day, values in by_day.items():
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        day_end = day_start + timedelta(days=1)
        covered = (min(day_end, end) - max(day_start, start)).total_seconds()
        clipped = [
            (max(a, day_start, start), min(b, day_end, end))
            for a, b in ticket_intervals
            if a < day_end and b > day_start
        ]
        downtime += min(covered, max(max(values.values()), _union_seconds(clipped)))
    pon_estimate = any(k[0] == ELEMENT_PON_PORT for k in keys)
    historical_path = end < datetime.now(UTC) - timedelta(days=1)
    estimate_reasons = []
    if pon_estimate:
        estimate_reasons.append(
            "PON availability uses daily working-ONT ratios rather than continuous outage history."
        )
    if historical_path:
        estimate_reasons.append(
            "Historical network path is not recorded; the current service path was used."
        )
    return CustomerAvailabilityResult(
        state=(
            AvailabilityState.estimated
            if estimate_reasons
            else AvailabilityState.confirmed
        ),
        expected_days=expected_days,
        observed_days=observed,
        period_seconds=period_seconds,
        downtime_seconds=min(period_seconds, max(0.0, downtime)),
        subscription_id=subscription.id,
        subscription_status=subscription.status.value,
        reason=" ".join(estimate_reasons) or None,
        evidence_sources=("customer_path", "availability_snapshots", "support_tickets"),
    )
