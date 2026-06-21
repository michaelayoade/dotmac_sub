"""Billing API webhook orchestration.

Inbound payment-provider webhooks (Paystack, Flutterwave) are money events: the
provider has already moved funds and is telling us about it. Providers treat an
HTTP 2xx as "delivered, do not retry"; any other status triggers their retry
schedule. So a processing failure must NOT return 2xx, or the event is silently
lost (we never record a payment the customer already made).

Two safeguards work together here:

1. **Dead-letter capture.** The raw, signature-verified payload is committed to
   ``payment_webhook_dead_letters`` *before* ingest is attempted, in its own
   transaction, so it survives an ingest rollback or a worker crash mid-ingest.
   On success the insurance row is deleted; on failure it is kept for replay.
2. **Honest status codes.** Transient/unexpected ingest errors return HTTP 5xx
   so the provider retries (``ingest`` is idempotent via ``idempotency_key``, so
   replays are safe). Deterministic rejections (bad data) return their 4xx and
   are parked as ``rejected`` for a human to inspect.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.models.billing import (
    PaymentProviderType,
    PaymentStatus,
    PaymentWebhookDeadLetter,
    PaymentWebhookDeadLetterStatus,
    TopupIntent,
)
from app.schemas.billing import PaymentProviderEventIngest
from app.services import billing as billing_service
from app.services.common import apply_pagination, get_by_id
from app.services.flutterwave import (
    verify_webhook_signature as verify_flutterwave_signature,
)
from app.services.paystack import verify_webhook_signature as verify_paystack_signature
from app.services.response import list_response
from app.services.topup_intents import set_topup_intent_status

logger = logging.getLogger(__name__)

_PROVIDER_TYPE_BY_NAME = {
    "paystack": PaymentProviderType.paystack,
    "flutterwave": PaymentProviderType.flutterwave,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _capture_dead_letter(
    db: Session,
    *,
    provider_type: str,
    event_type: str | None,
    external_id: str | None,
    idempotency_key: str | None,
    payload: dict,
) -> PaymentWebhookDeadLetter:
    """Durably record the inbound event before processing (own transaction).

    If the provider re-delivers a still-unresolved event we reuse its row and
    bump ``retry_count`` instead of piling up duplicates — but only when there
    is a real idempotency key to dedupe on.
    """
    existing: PaymentWebhookDeadLetter | None = None
    if idempotency_key:
        existing = (
            db.query(PaymentWebhookDeadLetter)
            .filter(PaymentWebhookDeadLetter.provider_type == provider_type)
            .filter(PaymentWebhookDeadLetter.idempotency_key == idempotency_key)
            .first()
        )
    if existing is not None:
        existing.status = PaymentWebhookDeadLetterStatus.received
        existing.event_type = event_type
        existing.external_id = external_id
        existing.payload = payload
        existing.retry_count = (existing.retry_count or 0) + 1
        existing.last_attempt_at = _now()
        row = existing
    else:
        row = PaymentWebhookDeadLetter(
            provider_type=provider_type,
            event_type=event_type,
            external_id=external_id,
            idempotency_key=idempotency_key,
            payload=payload,
            status=PaymentWebhookDeadLetterStatus.received,
            last_attempt_at=_now(),
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _resolve_dead_letter(
    db: Session,
    row: PaymentWebhookDeadLetter,
    status: PaymentWebhookDeadLetterStatus,
    *,
    error: str | None = None,
) -> None:
    """Move a captured row to a terminal/parked state (own transaction)."""
    row.status = status
    row.error = error
    row.last_attempt_at = _now()
    db.commit()


def _delete_dead_letter(db: Session, row: PaymentWebhookDeadLetter) -> None:
    """Drop the insurance row after a clean ingest."""
    db.delete(row)
    db.commit()


def _idempotency_key(provider_type: str, data: dict, ref_keys: Sequence[str]) -> str:
    ref = ""
    for key in ref_keys:
        value = data.get(key)
        if value:
            ref = str(value)
            break
    if not ref:
        ref = str(data.get("id", ""))
    return f"{provider_type}-{ref}"


@dataclass
class _Settlement:
    """Money facts extracted from a signature-verified provider payload."""

    status_hint: PaymentStatus
    amount: Decimal | None = None
    currency: str | None = None
    reference: str | None = None
    metadata: dict | None = None


def _extract_settlement(
    provider_type: str, event_type: str, data: dict
) -> _Settlement | None:
    """Pull amount/currency/metadata out of a charge event.

    Returns None for events that carry no settlement outcome (we still record
    them as provider events, we just don't move money for them). The payload
    is already signature-verified, so its amount is as trustworthy as a verify
    API response.
    """
    if provider_type == "paystack":
        if event_type != "charge.success":
            return None
        from app.services.paystack import kobo_to_naira

        metadata = data.get("metadata")
        return _Settlement(
            status_hint=PaymentStatus.succeeded,
            amount=kobo_to_naira(data.get("amount") or 0),
            currency=str(data.get("currency") or "NGN"),
            reference=str(data.get("reference") or "") or None,
            metadata=metadata if isinstance(metadata, dict) else {},
        )
    if provider_type == "flutterwave":
        # Flutterwave reuses charge.completed for both outcomes; the charge
        # status lives in data.status.
        if event_type != "charge.completed":
            return None
        metadata = data.get("meta")
        charge_status = str(data.get("status") or "").lower()
        if charge_status == "successful":
            return _Settlement(
                status_hint=PaymentStatus.succeeded,
                amount=Decimal(str(data.get("amount") or 0)),
                currency=str(data.get("currency") or "NGN"),
                reference=str(data.get("tx_ref") or "") or None,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        if charge_status == "failed":
            return _Settlement(
                status_hint=PaymentStatus.failed,
                reference=str(data.get("tx_ref") or "") or None,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
    return None


def _coerce_uuid(value) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _resolve_settlement_topup_intent(
    db: Session, settlement: _Settlement
) -> TopupIntent | None:
    """Load the top-up intent referenced by checkout metadata, if any.

    The intent is only trusted when its server-issued reference matches the
    transaction reference in the provider payload.
    """
    intent_id = _coerce_uuid((settlement.metadata or {}).get("topup_intent_id"))
    if intent_id is None:
        return None
    intent = db.get(TopupIntent, intent_id)
    if intent is None:
        return None
    if settlement.reference and intent.reference != settlement.reference:
        logger.warning(
            "Webhook topup intent %s reference mismatch (payload ref %s)",
            intent.id,
            settlement.reference,
        )
        return None
    return intent


def _finalize_webhook_topup_intent(
    db: Session, intent: TopupIntent, payment_id, amount: Decimal | None
) -> None:
    """Mark a top-up intent completed after webhook-driven settlement."""
    db.refresh(intent)
    if intent.completed_payment_id:
        return
    intent.completed_payment_id = payment_id
    intent.completed_at = _now()
    set_topup_intent_status(intent, "completed", source="webhook")
    if amount is not None:
        intent.actual_amount = amount
    db.commit()


def _process_webhook(
    *,
    db: Session,
    body: bytes,
    signature: str,
    provider_type: str,
    verify_signature: Callable[[bytes, str, Session], bool],
    ref_keys: Sequence[str],
) -> JSONResponse:
    if not verify_signature(body, signature, db):
        logger.warning("Invalid %s webhook signature", provider_type)
        return JSONResponse({"status": "invalid signature"}, status_code=400)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"status": "invalid JSON"}, status_code=400)

    event_type = payload.get("event", "unknown")
    data = payload.get("data", {}) or {}
    external_id = str(data.get("id", "")) or None
    idempotency_key = _idempotency_key(provider_type, data, ref_keys)
    logger.info("%s webhook: %s", provider_type, event_type)

    # 1. Durably capture BEFORE processing so nothing is lost on rollback/crash.
    dead_letter = _capture_dead_letter(
        db,
        provider_type=provider_type,
        event_type=event_type,
        external_id=external_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )

    provider = billing_service.payment_providers.get_by_type(
        db, _PROVIDER_TYPE_BY_NAME[provider_type]
    )
    if not provider:
        # Unusual after a verified signature, but never drop the event: park it
        # and ask the provider to retry until configuration catches up.
        logger.warning(
            "No %s payment provider configured; webhook parked for replay",
            provider_type,
        )
        _resolve_dead_letter(
            db,
            dead_letter,
            PaymentWebhookDeadLetterStatus.failed,
            error="No payment provider configured",
        )
        return JSONResponse({"status": "provider not configured"}, status_code=503)

    # Extract the money facts (amount/currency/invoice/top-up intent) from the
    # signature-verified payload so ingest can actually settle the payment.
    # Without these, a customer who paid but never returned to the verify URL
    # would have captured funds and an invoice that never gets marked paid.
    settlement = _extract_settlement(provider_type, event_type, data)
    topup_intent: TopupIntent | None = None
    ingest_payload = PaymentProviderEventIngest(
        provider_id=provider.id,
        event_type=event_type,
        external_id=external_id or "",
        idempotency_key=idempotency_key,
        payload=payload,
    )
    if settlement is not None:
        ingest_payload.status_hint = settlement.status_hint
        metadata = settlement.metadata or {}
        invoice_id = _coerce_uuid(metadata.get("invoice_id"))
        account_id = _coerce_uuid(metadata.get("account_id"))
        billing_account_id = _coerce_uuid(metadata.get("billing_account_id"))
        topup_intent = _resolve_settlement_topup_intent(db, settlement)
        if topup_intent is not None and topup_intent.account_id is not None:
            account_id = topup_intent.account_id
        # Reseller-consolidated top-ups carry billing_account_id (and no
        # account_id) on the intent. Without forwarding it, the webhook payment
        # posts with billing_account_id NULL and never credits the billing
        # account / settles member invoices — the cutover posting gap. (#cutover)
        if topup_intent is not None and topup_intent.billing_account_id is not None:
            billing_account_id = topup_intent.billing_account_id
        if (
            settlement.status_hint == PaymentStatus.succeeded
            and settlement.amount is not None
            and settlement.amount > Decimal("0.00")
        ):
            ingest_payload.amount = settlement.amount
            ingest_payload.currency = settlement.currency
            ingest_payload.invoice_id = invoice_id
            ingest_payload.account_id = account_id
            ingest_payload.billing_account_id = billing_account_id

    # 2. Process inside a SAVEPOINT so a failure rolls back only ingest's own
    #    partial writes, leaving the committed dead-letter row (and the outer
    #    transaction) intact — no full rollback that would also discard the
    #    capture. ingest() self-commits on success, releasing the savepoint.
    #    Honest status codes drive the provider's retry behaviour.
    nested = db.begin_nested()
    try:
        event = billing_service.payment_provider_events.ingest(db, ingest_payload)
    except HTTPException as exc:
        # Deterministic rejection (e.g. invoice/account mismatch). Retrying the
        # same payload won't help — surface the 4xx and park for human review.
        if nested.is_active:
            nested.rollback()
        logger.warning(
            "%s webhook rejected (%s): %s",
            provider_type,
            exc.status_code,
            exc.detail,
        )
        _resolve_dead_letter(
            db,
            dead_letter,
            PaymentWebhookDeadLetterStatus.rejected,
            error=f"{exc.status_code}: {exc.detail}",
        )
        return JSONResponse(
            {"status": "rejected", "detail": exc.detail},
            status_code=exc.status_code,
        )
    except Exception as exc:
        # Transient/unexpected. Ask the provider to retry; ingest is idempotent.
        if nested.is_active:
            nested.rollback()
        logger.error(
            "%s webhook processing error: %s", provider_type, exc, exc_info=True
        )
        _resolve_dead_letter(
            db,
            dead_letter,
            PaymentWebhookDeadLetterStatus.failed,
            error=str(exc),
        )
        return JSONResponse({"status": "error"}, status_code=500)

    # 3. Post-settlement bookkeeping (best-effort: the money is recorded; a
    #    failure here must not make the provider retry and is recoverable via
    #    the customer's own verify flow or the reconciliation sweep).
    if (
        settlement is not None
        and settlement.status_hint == PaymentStatus.succeeded
        and event.payment_id is not None
    ):
        if topup_intent is not None:
            try:
                _finalize_webhook_topup_intent(
                    db, topup_intent, event.payment_id, settlement.amount
                )
            except Exception:
                logger.warning(
                    "Failed to finalize topup intent after webhook settlement",
                    exc_info=True,
                )
                db.rollback()
        account_for_restore = ingest_payload.account_id or (
            topup_intent.account_id if topup_intent is not None else None
        )
        if account_for_restore is not None:
            # Invoice-paid restores already ran inside the payment pipeline;
            # this additionally covers prepaid/balance-based suspensions.
            try:
                from app.services import collections as collections_service

                collections_service.restore_account_services(
                    db, str(account_for_restore)
                )
            except Exception:
                logger.warning(
                    "Failed to auto-restore after webhook settlement for %s",
                    account_for_restore,
                    exc_info=True,
                )
                db.rollback()

    # 4. Recorded as a PaymentProviderEvent — the insurance row is redundant.
    _delete_dead_letter(db, dead_letter)
    return JSONResponse({"status": "ok"}, status_code=200)


def process_paystack_webhook(
    *, db: Session, body: bytes, signature: str
) -> JSONResponse:
    return _process_webhook(
        db=db,
        body=body,
        signature=signature,
        provider_type="paystack",
        verify_signature=verify_paystack_signature,
        ref_keys=("reference",),
    )


def process_flutterwave_webhook(
    *, db: Session, body: bytes, signature: str
) -> JSONResponse:
    return _process_webhook(
        db=db,
        body=body,
        signature=signature,
        provider_type="flutterwave",
        verify_signature=verify_flutterwave_signature,
        ref_keys=("tx_ref",),
    )


def list_payment_webhook_dead_letters(
    db: Session,
    *,
    provider_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List parked webhook events, newest first. For the ops replay surface."""
    query = db.query(PaymentWebhookDeadLetter)
    if provider_type:
        query = query.filter(PaymentWebhookDeadLetter.provider_type == provider_type)
    if status:
        try:
            status_enum = PaymentWebhookDeadLetterStatus(status)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Unknown status: {status}"
            ) from exc
        query = query.filter(PaymentWebhookDeadLetter.status == status_enum)
    query = query.order_by(PaymentWebhookDeadLetter.received_at.desc())
    items = apply_pagination(query, limit, offset).all()
    return list_response(items, limit, offset)


def replay_payment_webhook_dead_letter(
    db: Session, dead_letter_id: str
) -> PaymentWebhookDeadLetter:
    """Reprocess a parked webhook through ingest using its stored payload.

    For ops/cron use. Signature was already verified at receipt, so we go
    straight to ingest. ``ingest`` is idempotent, so replaying an event that
    actually did land is a no-op. On success the row is marked ``replayed``.
    """
    row = get_by_id(db, PaymentWebhookDeadLetter, dead_letter_id)
    if not row:
        raise HTTPException(status_code=404, detail="Dead-letter event not found")

    provider_enum = _PROVIDER_TYPE_BY_NAME.get(row.provider_type)
    if provider_enum is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown provider type: {row.provider_type}"
        )
    provider = billing_service.payment_providers.get_by_type(db, provider_enum)
    if not provider:
        raise HTTPException(
            status_code=400,
            detail=f"No {row.provider_type} payment provider configured",
        )

    payload = row.payload or {}
    nested = db.begin_nested()
    try:
        billing_service.payment_provider_events.ingest(
            db,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                event_type=row.event_type or payload.get("event", "unknown"),
                external_id=row.external_id or "",
                idempotency_key=row.idempotency_key,
                payload=payload,
            ),
        )
    except HTTPException:
        if nested.is_active:
            nested.rollback()
        _resolve_dead_letter(
            db, row, PaymentWebhookDeadLetterStatus.rejected, error="replay rejected"
        )
        raise
    except Exception as exc:
        if nested.is_active:
            nested.rollback()
        _resolve_dead_letter(
            db, row, PaymentWebhookDeadLetterStatus.failed, error=str(exc)
        )
        raise

    _resolve_dead_letter(db, row, PaymentWebhookDeadLetterStatus.replayed)
    return row
