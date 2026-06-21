"""Customer-safe connection status (Phase 3, selfcare).

Maps the internal topology path to a coarse, customer-safe view: the
basestation name + a {healthy, degraded, outage, unknown} status. Deliberately
exposes NO internal IPs, device names, node ids, or gap internals — the
customer sees "you're connected via <BTS>, status: healthy", nothing more.
Reads the warmed live_status cache; never live-polls Zabbix.

Flapping guard: the warmed ``live_status`` is a single Zabbix snapshot with no
smoothing, so a node that drops for one poll would otherwise flip the customer
straight to "outage". A *bad* state (degraded/outage) is only surfaced once it
has persisted past a dwell window (``live_status_at`` marks when the node
entered its current state — stamped on change only by the warmer). Good news
(healthy/unknown) and operator-declared incidents surface immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.models.domain_settings import SettingDomain
from app.services.settings_spec import resolve_value
from app.services.topology.customer_path import resolve_customer_path

# internal live_status -> customer-safe status
_SAFE = {"up": "healthy", "problem": "degraded", "down": "outage"}
# Customer-facing bad states that must persist before we surface them.
_DEBOUNCED = frozenset({"degraded", "outage"})
_DEFAULT_DWELL_SECONDS = 360


def _dwell_seconds(session: Session) -> int:
    raw = resolve_value(
        session,
        SettingDomain.network_monitoring,
        "connection_status_outage_dwell_seconds",
    )
    if raw is None:
        return _DEFAULT_DWELL_SECONDS
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return _DEFAULT_DWELL_SECONDS


def customer_connection_status(
    session: Session,
    subscription: Subscription,
    *,
    now: datetime | None = None,
) -> dict:
    """Return {basestation, status} for the customer's connection.

    ``status`` is healthy/degraded/outage/unknown; ``basestation`` is the site
    name or None. Nothing internal (IPs, device names, gap reasons) leaks.
    """
    from app.services.topology.outage import open_incident_for_path

    path = resolve_customer_path(session, subscription)
    node = path.node
    live = node.live_status if node is not None else None
    known_outage = open_incident_for_path(session, path) is not None

    if known_outage:
        # An operator-declared outage is authoritative — surface immediately.
        status = "outage"
    else:
        raw = _SAFE.get(live or "", "unknown")
        if raw in _DEBOUNCED and not _bad_state_has_settled(session, node, now):
            # Transient blip — don't cry outage over a single flapping poll.
            status = "healthy"
        else:
            status = raw
    return {
        "basestation": path.basestation.name if path.basestation is not None else None,
        "status": status,
        "known_outage": known_outage,
    }


def _bad_state_has_settled(session: Session, node, now: datetime | None) -> bool:
    """True once the node's current (bad) live_status has held past the dwell."""
    since = node.live_status_at if node is not None else None
    if since is None:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return (now - since) >= timedelta(seconds=_dwell_seconds(session))
