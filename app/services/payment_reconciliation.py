"""Recover gateway payments whose customer never completed the verify leg.

A top-up (or saved-card charge made at intent time) can be captured at the
gateway while the customer closes the webview/browser before the redirect, and
the provider's webhook can be lost or misconfigured. The TopupIntent row is the
durable record that money was *expected*; this sweep re-queries the gateway for
stale pending intents and settles any that actually succeeded, using the same
payment pipeline (and the same idempotency guards) as the verify and webhook
paths.
"""

from __future__ import annotations

import logging
from uuid import UUID as _UUID
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentStatus, TopupIntent
from app.models.domain_settings import SettingDomain
from app.schemas.billing import PaymentAllocationApply, PaymentCreate
from app.services import billing as billing_service
from app.services import settings_spec
from app.services.billing._common import lock_account
from app.services.collections import restore_account_services
from app.services.common import round_money, to_decimal
from app.services.customer_portal_flow_payments import _provider_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.payment_gateway_adapter import payment_gateway_adapter
from app.services.topup_intents import set_topup_intent_status

logger = logging.getLogger(__name__)

# How long after expiry an unconfirmed intent is parked as expired. The
# gateway can confirm a charge a little after our checkout TTL, so give it a
# generous grace before giving up.
_EXPIRE_GRACE = timedelta(days=1)

# Only providers with an online verify API belong in this sweep. Manual
# methods (e.g. ``direct_bank_transfer``) settle via proof upload and have no
# transaction to verify — feeding them to the gateway adapter just 400s.
_GATEWAY_PROVIDERS = ("paystack", "flutterwave")

# Gateway HTTP statuses that mean "no such (or no charged) transaction" — the
# customer never completed checkout. Semantically identical to a not-successful
# verify, so these expire rather than counting as retryable errors (otherwise
# abandoned intents re-error every run and jam the bounded sweep).
_NOT_FOUND_STATUSES = (400, 404)
DEFAULT_STALE_MINUTES = 15
DEFAULT_MAX_AGE_DAYS = 7
SessionLocal = db_session_adapter.create_session


def _resolve_positive_int_setting(
    db: Session,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = settings_spec.resolve_value(db, SettingDomain.billing, key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _park_if_expired(db: Session, intent: TopupIntent, now: datetime) -> bool:
    """Mark an unconfirmed intent ``expired`` once well past its TTL. Returns
    True if it was parked."""
    expires_at = intent.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at and now > expires_at + _EXPIRE_GRACE:
        set_topup_intent_status(intent, "expired", source="reconcile_expiry")
        db.commit()
        return True
    return False


def _intent_allocations(
    intent: TopupIntent, amount
) -> list[PaymentAllocationApply] | None:
    """Allocate a reconciled payment the way the customer asked us to.

    The intent is the authoritative record of what the payment was *for*: an
    invoice checkout stamps ``metadata_["invoice_id"]`` alongside
    ``payment_flow="invoice_payment"``. The happy path
    (``verify_and_record_payment``) allocates explicitly to that invoice.

    Reconciliation is a *repair* path: it must converge on the same outcome, not
    a different one. Returning ``None`` here would auto-allocate oldest-invoice-
    first, so a customer who paid invoice #7 could have the recovered payment
    applied to invoice #3 -- leaving the invoice they actually paid still open,
    inviting a second payment, and recording in the ledger that they settled a
    bill they never chose. The money is not lost, but it lands in the wrong place.

    Genuine top-ups have no target invoice and keep auto-allocation.
    """
    metadata = intent.metadata_ or {}
    if str(metadata.get("payment_flow")) != "invoice_payment":
        return None
    invoice_id = metadata.get("invoice_id")
    if not invoice_id:
        # An invoice checkout with no recorded invoice is a bug upstream; do not
        # silently guess which bill to settle.
        logger.warning(
            "Invoice-payment intent %s has no invoice_id in metadata; falling "
            "back to auto-allocation",
            intent.id,
        )
        return None
    try:
        target = _UUID(str(invoice_id))
    except (TypeError, ValueError):
        logger.warning(
            "Invoice-payment intent %s has an unparseable invoice_id %r; falling "
            "back to auto-allocation",
            intent.id,
            invoice_id,
        )
        return None
    return [PaymentAllocationApply(invoice_id=target, amount=amount)]


def _settle_intent(
    db: Session,
    intent: TopupIntent,
    *,
    external_id: str,
    amount,
    currency: str,
    memo: str,
    now: datetime,
) -> bool:
    """Record (or link) the payment for a confirmed intent. True if recovered."""
    existing = db.scalars(
        select(Payment).where(Payment.external_id == external_id)
    ).first()
    created = False
    if existing is not None:
        payment = existing
    else:
        payment = billing_service.payments.create(
            db,
            PaymentCreate(
                account_id=intent.account_id,
                billing_account_id=intent.billing_account_id
                if intent.account_id is None
                else None,
                amount=amount,
                currency=currency,
                status=PaymentStatus.succeeded,
                provider_id=_provider_uuid(db, intent.provider_type),
                external_id=external_id,
                memo=memo,
                # Honour the customer's instruction recorded on the intent; only
                # genuine top-ups auto-allocate (invoices first, rest credit).
                allocations=_intent_allocations(intent, amount),
            ),
        )
        created = True
    intent.completed_payment_id = payment.id
    set_topup_intent_status(intent, "completed", source="reconcile_settle")
    intent.completed_at = now
    intent.actual_amount = amount
    intent.external_id = external_id
    db.commit()
    return created


def reconcile_pending_topups(
    db: Session,
    *,
    older_than_minutes: int | None = None,
    max_age_days: int | None = None,
    limit: int = 50,
) -> dict[str, int]:
    """Sweep stale pending top-up intents against the gateway verify API."""
    older_than_minutes = (
        int(older_than_minutes)
        if older_than_minutes is not None
        else _resolve_positive_int_setting(
            db,
            "topup_reconciliation_stale_minutes",
            DEFAULT_STALE_MINUTES,
            minimum=1,
            maximum=1440,
        )
    )
    max_age_days = (
        int(max_age_days)
        if max_age_days is not None
        else _resolve_positive_int_setting(
            db,
            "topup_reconciliation_max_age_days",
            DEFAULT_MAX_AGE_DAYS,
            minimum=1,
            maximum=30,
        )
    )
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=older_than_minutes)
    oldest = now - timedelta(days=max_age_days)
    intents = db.scalars(
        select(TopupIntent)
        .where(TopupIntent.status == "pending")
        .where(TopupIntent.completed_payment_id.is_(None))
        .where(TopupIntent.provider_type.in_(_GATEWAY_PROVIDERS))
        .where(TopupIntent.created_at < stale_before)
        .where(TopupIntent.created_at > oldest)
        .order_by(TopupIntent.created_at.asc())
        .limit(limit)
    ).all()

    checked = recovered = linked = expired = errors = 0
    for intent in intents:
        checked += 1
        try:
            tx = payment_gateway_adapter.verify(
                db,
                provider_type=intent.provider_type,
                reference=intent.reference,
            )
        except ValueError:
            # Gateway says not successful (abandoned, declined, or unknown
            # reference). Once well past expiry, stop re-checking it.
            if _park_if_expired(db, intent, now):
                expired += 1
            continue
        except httpx.HTTPStatusError as exc:
            # 400/404 == the gateway has no such (or no charged) transaction:
            # the customer never completed checkout. Treat exactly like the
            # not-successful ValueError path so it expires instead of erroring
            # forever. Auth (401/403) and 5xx stay errors — they're config or
            # transient problems we want surfaced and retried.
            if exc.response.status_code in _NOT_FOUND_STATUSES:
                if _park_if_expired(db, intent, now):
                    expired += 1
                continue
            logger.warning(
                "Top-up reconciliation: gateway verify failed for intent %s (http %s)",
                intent.id,
                exc.response.status_code,
                exc_info=True,
            )
            errors += 1
            continue
        except Exception:
            logger.warning(
                "Top-up reconciliation: gateway verify failed for intent %s",
                intent.id,
                exc_info=True,
            )
            errors += 1
            continue

        try:
            if intent.account_id is not None:
                lock_account(db, str(intent.account_id))
                db.refresh(intent)
                if intent.completed_payment_id:
                    continue
            created = _settle_intent(
                db,
                intent,
                external_id=tx.external_id,
                amount=round_money(to_decimal(tx.amount)),
                currency=tx.currency,
                memo=f"{tx.memo_prefix} top-up reconciliation ref: {intent.reference}",
                now=now,
            )
            if created:
                recovered += 1
                logger.info(
                    "Top-up reconciliation recovered payment for intent %s "
                    "(account %s, amount %s)",
                    intent.id,
                    intent.account_id or intent.billing_account_id,
                    intent.actual_amount,
                )
            else:
                linked += 1
            if intent.account_id is not None:
                try:
                    from app.services.billing.reconcile_unposted import (
                        settle_prepaid_draft_invoices_from_credit,
                    )

                    settled = settle_prepaid_draft_invoices_from_credit(
                        db, str(intent.account_id), run_at=now
                    )
                    if settled.changed:
                        logger.info(
                            "Top-up reconciliation settled %d prepaid draft "
                            "invoice(s) for account %s",
                            len(settled.invoices_settled),
                            intent.account_id,
                        )
                        db.commit()
                    restore_account_services(db, str(intent.account_id))
                except Exception:
                    db.rollback()
                    logger.warning(
                        "Top-up reconciliation: restore failed for account %s",
                        intent.account_id,
                        exc_info=True,
                    )
        except Exception:
            db.rollback()
            logger.exception(
                "Top-up reconciliation: settling intent %s failed", intent.id
            )
            errors += 1

    return {
        "checked": checked,
        "recovered": recovered,
        "linked": linked,
        "expired": expired,
        "errors": errors,
    }


def reconcile_topups_scheduled() -> dict[str, int]:
    """Scheduled top-up reconciliation entry point."""
    logger.info("Starting top-up payment reconciliation sweep")
    session = SessionLocal()
    try:
        result = reconcile_pending_topups(session)
        logger.info(
            "Top-up reconciliation completed: checked=%d recovered=%d "
            "linked=%d expired=%d errors=%d",
            result.get("checked", 0),
            result.get("recovered", 0),
            result.get("linked", 0),
            result.get("expired", 0),
            result.get("errors", 0),
        )
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
