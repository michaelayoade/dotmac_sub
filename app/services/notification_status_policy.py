"""Account-status gate for customer notifications.

Independent of per-subscriber *preferences*, some account states must not
receive customer mail at all — sending to them is wrong and damages sender
reputation:

- **Terminal** (``canceled``/``disabled``) — churned or closed. Dead/stale
  mailboxes → bounces. They receive **nothing**.
- **Walled** (``suspended``/``blocked``) — still customers, but service is cut.
  They receive only actionable categories (billing/dunning, account/security,
  service-status) so they can pay and get back online; non-essential mail
  (usage/FUP for a service they can't use) is suppressed.
- **Everything else** (``active``/``new``/``delinquent``) — unaffected.

This is a HARD gate: it overrides preferences (a "subscribed" preference cannot
re-enable mail to a canceled account). It only applies to subscriber-scoped
notifications — operator/admin alerts with no subscriber are never gated. The
``NotificationHandler`` consults :func:`status_allows_notification`, behind the
``notification.status_gate_enabled`` kill-switch (default on).
"""

from __future__ import annotations

from app.models.subscriber import SubscriberStatus

# Terminal account states never receive any customer notification.
_TERMINAL_STATUSES = frozenset(
    {SubscriberStatus.canceled, SubscriberStatus.disabled}
)

# Walled (service-cut) account states receive only these actionable categories.
_WALLED_STATUSES = frozenset(
    {SubscriberStatus.suspended, SubscriberStatus.blocked}
)
_WALLED_ALLOWED_CATEGORIES = frozenset({"billing", "account", "service"})


def status_allows_notification(
    status: SubscriberStatus | None, category: str | None
) -> bool:
    """Whether an account in ``status`` may receive a ``category`` notification.

    ``status`` None (unknown subscriber) is allowed — the gate only restricts
    known terminal/walled accounts and must not silently drop mail for states it
    can't classify.
    """
    if status is None:
        return True
    if status in _TERMINAL_STATUSES:
        return False
    if status in _WALLED_STATUSES:
        return (category or "").strip().lower() in _WALLED_ALLOWED_CATEGORIES
    return True
