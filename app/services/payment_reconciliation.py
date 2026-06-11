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
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentStatus, TopupIntent
from app.schemas.billing import PaymentCreate
from app.services import billing as billing_service
from app.services.billing._common import lock_account
from app.services.collections import restore_account_services
from app.services.common import round_money, to_decimal
from app.services.customer_portal_flow_payments import _provider_uuid
from app.services.payment_gateway_adapter import payment_gateway_adapter

logger = logging.getLogger(__name__)

# How long after expiry an unconfirmed intent is parked as expired. The
# gateway can confirm a charge a little after our checkout TTL, so give it a
# generous grace before giving up.
_EXPIRE_GRACE = timedelta(days=1)


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
                allocations=None,  # auto-allocate: invoices first, rest credit
            ),
        )
        created = True
    intent.completed_payment_id = payment.id
    intent.status = "completed"
    intent.completed_at = now
    intent.actual_amount = amount
    intent.external_id = external_id
    db.commit()
    return created


def reconcile_pending_topups(
    db: Session,
    *,
    older_than_minutes: int = 15,
    max_age_days: int = 7,
    limit: int = 50,
) -> dict[str, int]:
    """Sweep stale pending top-up intents against the gateway verify API."""
    now = datetime.now(UTC)
    stale_before = now - timedelta(minutes=older_than_minutes)
    oldest = now - timedelta(days=max_age_days)
    intents = db.scalars(
        select(TopupIntent)
        .where(TopupIntent.status == "pending")
        .where(TopupIntent.completed_payment_id.is_(None))
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
            expires_at = intent.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at and now > expires_at + _EXPIRE_GRACE:
                intent.status = "expired"
                db.commit()
                expired += 1
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
                    restore_account_services(db, str(intent.account_id))
                except Exception:
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
