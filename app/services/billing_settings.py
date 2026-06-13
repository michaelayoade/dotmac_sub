"""Helpers for resolving billing settings with legacy fallbacks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services import settings_spec


def billing_enabled(db: Session, *, default: bool = True) -> bool:
    """Master switch for local billing automation.

    While the upstream biller (Splynx) remains authoritative, this is set to
    ``false`` in prod so the local runners stay inert. It gates every task that
    *acts on customers* off local billing state — invoicing, autopay charges,
    dunning, prepaid enforcement, payment-arrangement checks, and subscription
    expiry — so they all activate together at cutover and none can charge,
    suspend, or expire an account before then. Resolved via ``settings_spec``
    (env fallback included) to match the invoice-cycle kill-switch.
    """
    value = settings_spec.resolve_value(db, SettingDomain.billing, "billing_enabled")
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def check_billing_switch(db: Session) -> dict:
    """Invariant check on the ``billing_enabled`` master switch.

    ``billing_enabled`` flipping to true unexpectedly is what let the local
    runner generate phantom invoices — a config-integrity failure, not a code
    bug, so the void cleaned the symptom, not the mechanism. This compares the
    live switch against a pinned *expected* value (``billing_enabled_expected``
    / env ``BILLING_ENABLED_EXPECTED``, default false pre-cutover). At cutover,
    set the expected value to true in the same change that enables billing.

    Returns a dict; callers should alert when ``ok`` is false.
    """
    import os

    actual = billing_enabled(db, default=False)
    # Read the pinned expected value directly (it is not a registered spec key):
    # a DomainSetting row wins, else the BILLING_ENABLED_EXPECTED env, else false.
    expected_raw = _setting_value(db, "billing_enabled_expected")
    if expected_raw is None:
        expected_raw = os.getenv("BILLING_ENABLED_EXPECTED")
    if expected_raw is None:
        expected = False
    elif isinstance(expected_raw, bool):
        expected = expected_raw
    else:
        expected = str(expected_raw).strip().lower() in {"1", "true", "yes", "on"}
    return {"ok": actual == expected, "expected": expected, "actual": actual}


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _setting_value(db: Session, key: str) -> object | None:
    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == SettingDomain.billing)
        .where(DomainSetting.key == key)
        .where(DomainSetting.is_active.is_(True))
    )
    setting = db.scalars(stmt).first()
    if not setting:
        return None
    return setting.value_json if setting.value_json is not None else setting.value_text


def resolve_payment_due_days(
    db: Session,
    default: int = 14,
    subscriber: object | None = None,
) -> int:
    """Resolve payment due days: subscriber override > global setting > legacy keys.

    Args:
        db: Database session.
        default: Fallback if no setting is found.
        subscriber: Optional subscriber — if they have ``payment_due_days``
            set, that value takes priority over the global setting.
    """
    # Subscriber-level override takes priority
    sub_due_days = getattr(subscriber, "payment_due_days", None)
    if sub_due_days is not None:
        return max(_coerce_int(sub_due_days, default), 0)

    canonical = _setting_value(db, "payment_due_days")
    if canonical is not None:
        return max(_coerce_int(canonical, default), 0)

    legacy_invoice = _setting_value(db, "invoice_due_days")
    if legacy_invoice is not None:
        return max(_coerce_int(legacy_invoice, default), 0)

    legacy_terms = _setting_value(db, "default_payment_terms_days")
    if legacy_terms is not None:
        return max(_coerce_int(legacy_terms, default), 0)

    return default
