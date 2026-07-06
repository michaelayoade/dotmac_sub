"""Outage notification send-path — outage classifier P4 (design §P4).

Compose the customer-facing outage notifications and split them the way that
matters (design §5): an **area outage** gets ONE "known outage, we're on it"
message per affected customer, while an isolated **last-mile** fault gets that
customer's specific advice. Telling 200 customers on a cut splitter to "reboot
your router" is exactly the failure this split prevents.

**This module never sends.** It is gated on ``OUTAGE_NOTIFY_ENABLED`` (default
off) and only ever *plans* — ``plan_outage_notifications`` returns the composed
plan (recipients, channel, sample bodies) for review. The live dispatch path is
deliberately unimplemented: it is gated on the operator's comms policy, not on
data (design §P4 "notification send-path (gated on comms policy, not data)").

Bodies are the customer-safe strings from ``connection_status`` — no node
names, no signal values, nothing about other customers. Recipient targeting and
opt-out honour the existing ``customer_notification_policy``.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models.catalog import Subscription
from app.services.customer_notification_policy import (
    is_notification_enabled_for_subscriber,
)
from app.services.notification_adapter import NotificationCategory, NotificationChannel
from app.services.topology import connection_status
from app.services.topology.connection_status import STATE_CONNECTED, STATE_OUTAGE

logger = logging.getLogger(__name__)

# Channel + category the outage notifications would use (for opt-out checks).
OUTAGE_NOTIFY_CHANNEL = NotificationChannel.sms
OUTAGE_NOTIFY_CATEGORY = NotificationCategory.service

# Don't re-plan the same area boundary within this window — a flapping boundary
# must not spam (design §7.6/§7.7). P4b debounces in-memory only; the live
# send-path will need this persisted (a table) before it can dispatch — see the
# module docstring. No migration in P4b.
DEBOUNCE_WINDOW = timedelta(hours=1)

# boundary_id -> last planned datetime. Module-level so repeated planning runs
# in one process dedup; tests pass their own ``debounce_state`` for isolation.
_DEBOUNCE_CACHE: dict[uuid.UUID, datetime] = {}


def _enabled() -> bool:
    return bool(getattr(settings, "outage_notify_enabled", False))


def _debounced(
    boundary_id: uuid.UUID, now: datetime, state: dict[uuid.UUID, datetime]
) -> bool:
    last = state.get(boundary_id)
    return last is not None and (now - last) < DEBOUNCE_WINDOW


def plan_outage_notifications(
    session: Session,
    subscription_ids,
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    debounce_state: dict[uuid.UUID, datetime] | None = None,
) -> dict:
    """Compose (never send) the outage notification plan for these customers.

    ``subscription_ids`` is the candidate set — typically the currently-troubled
    customers an outage console surfaces. Each is assessed; area-outage customers
    are grouped by their (internal) boundary and get the area message, everyone
    else with a fault gets their last-mile message, connected customers are
    skipped. Opt-out and debounce are applied; the result is a review plan::

        {
          enabled, dry_run, dispatched: False, generated_at,
          area_outages: [{recipients, suppressed_optout, debounced, channel,
                          sample_body}],
          per_customer: [{verdict, recipients, suppressed_optout, channel,
                          sample_body}],
          would_send_total,
        }

    ``would_send_total`` is 0 whenever the feature is disabled. Nothing is ever
    dispatched here regardless of flags (``dispatched`` is always False).
    """
    now = now or datetime.now(UTC)
    enabled = _enabled()
    state = _DEBOUNCE_CACHE if debounce_state is None else debounce_state

    ids = list(subscription_ids)
    subs = (
        session.query(Subscription).filter(Subscription.id.in_(ids)).all()
        if ids
        else []
    )

    # boundary_id -> {"recipients": set, "suppressed": int, "body": str}
    area: dict[uuid.UUID, dict] = defaultdict(
        lambda: {"recipients": set(), "suppressed": 0, "body": ""}
    )
    # verdict -> {"recipients": set, "suppressed": int, "body": str}
    per_customer: dict[str, dict] = defaultdict(
        lambda: {"recipients": set(), "suppressed": 0, "body": ""}
    )

    for sub in subs:
        a = connection_status.assess(session, sub, now=now)
        if a.state == STATE_CONNECTED:
            continue
        opted_in = is_notification_enabled_for_subscriber(
            session,
            subscriber_id=sub.subscriber_id,
            # Pass the plain values: the policy lower-cases str(category) to build
            # its "<category>_notifications" key, so the enum's qualified name
            # would miss the preference. Values ("service"/"sms") match exactly.
            channel=OUTAGE_NOTIFY_CHANNEL.value,
            category=OUTAGE_NOTIFY_CATEGORY.value,
        )
        if a.state == STATE_OUTAGE and a.area_boundary_id is not None:
            bucket = area[a.area_boundary_id]
        else:
            bucket = per_customer[a.verdict]
        bucket["body"] = a.message
        if opted_in:
            bucket["recipients"].add(sub.subscriber_id)
        else:
            bucket["suppressed"] += 1

    channel = OUTAGE_NOTIFY_CHANNEL.value
    would_send = 0

    area_out = []
    for boundary_id, b in area.items():
        debounced = _debounced(boundary_id, now, state)
        n = len(b["recipients"])
        if enabled and not debounced and n:
            would_send += n
            # Record the plan so a repeat within the window is debounced. Only
            # stamp when we'd actually send (enabled) — a disabled preview run
            # must not consume the debounce window.
            state[boundary_id] = now
        area_out.append(
            {
                "recipients": n,
                "suppressed_optout": b["suppressed"],
                "debounced": debounced,
                "channel": channel,
                "sample_body": b["body"],
            }
        )

    per_out = []
    for verdict, b in per_customer.items():
        n = len(b["recipients"])
        if enabled and n:
            would_send += n
        per_out.append(
            {
                "verdict": verdict,
                "recipients": n,
                "suppressed_optout": b["suppressed"],
                "channel": channel,
                "sample_body": b["body"],
            }
        )

    if not enabled:
        # Feature off: pure preview, nothing would go out.
        would_send = 0

    plan = {
        "enabled": enabled,
        "dry_run": dry_run,
        "dispatched": False,  # P4b never dispatches — live path gated on policy
        "generated_at": now.isoformat(),
        "area_outages": area_out,
        "per_customer": per_out,
        "would_send_total": would_send,
    }
    logger.info(
        "outage-notify plan: enabled=%s areas=%d per_customer=%d would_send=%d "
        "(dispatched=False — live send-path gated on comms policy)",
        enabled,
        len(area_out),
        len(per_out),
        would_send,
    )
    return plan
