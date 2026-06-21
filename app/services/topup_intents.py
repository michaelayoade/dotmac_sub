"""Top-up intent status — formalised enum + a validated, observable setter.

``TopupIntent.status`` is a free-form ``String(20)`` written ad-hoc from four
services (reconciler, webhook, customer portal, reseller portal). This module
formalises the value set and routes every write through ``set_topup_intent_status``
so:

* garbage / unknown values are rejected at the write boundary, and
* a terminal intent (``expired``/``canceled``) being completed by a **late but
  real** gateway payment is recorded (``topup_intent_terminal_recovery``) rather
  than silently flipped.

NOTE — deliberately NOT a terminal lock. A gateway payment can legitimately
arrive after the sweep expired the intent (or the customer started a replacement,
canceling the old one); refusing to complete it would drop real money. Double
crediting is already prevented by the per-intent ``completed_payment_id`` /
per-payment ``external_id`` idempotency in the completion paths.
"""

from __future__ import annotations

import logging
from enum import Enum

from app.models.billing import TopupIntent

logger = logging.getLogger(__name__)


class TopupIntentStatus(str, Enum):
    pending = "pending"
    submitted = "submitted"
    completed = "completed"
    expired = "expired"
    canceled = "canceled"


_VALID_TOPUP_STATUSES: frozenset[str] = frozenset(s.value for s in TopupIntentStatus)
_TOPUP_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        TopupIntentStatus.expired.value,
        TopupIntentStatus.canceled.value,
    }
)


def set_topup_intent_status(
    intent: TopupIntent, new_status: TopupIntentStatus | str, *, source: str
) -> bool:
    """Validated write to ``TopupIntent.status``. Returns True if it changed.

    Rejects unknown values. Allows every transition (money safety — see module
    docstring) but emits ``topup_intent_terminal_recovery`` when a terminal
    intent is completed, so late-payment recoveries are observable.
    """
    raw = (
        new_status.value
        if isinstance(new_status, TopupIntentStatus)
        else str(new_status).strip()
    )
    if raw not in _VALID_TOPUP_STATUSES:
        raise ValueError(f"Invalid top-up intent status: {new_status!r}")
    current = intent.status
    if current == raw:
        return False
    if current in _TOPUP_TERMINAL_STATUSES and raw == TopupIntentStatus.completed.value:
        logger.warning(
            "topup_intent_terminal_recovery",
            extra={
                "event": "topup_intent_terminal_recovery",
                "intent_id": str(getattr(intent, "id", None)),
                "from": current,
                "to": raw,
                "source": source,
            },
        )
    intent.status = raw
    return True
