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
from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.models.billing import (
    PaymentProviderType,
    PaymentWebhookDeadLetter,
    PaymentWebhookDeadLetterStatus,
)
from app.schemas.billing import PaymentProviderEventIngest
from app.services import billing as billing_service
from app.services.common import get_by_id
from app.services.flutterwave import (
    verify_webhook_signature as verify_flutterwave_signature,
)
from app.services.paystack import verify_webhook_signature as verify_paystack_signature

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

    # 2. Process inside a SAVEPOINT so a failure rolls back only ingest's own
    #    partial writes, leaving the committed dead-letter row (and the outer
    #    transaction) intact — no full rollback that would also discard the
    #    capture. ingest() self-commits on success, releasing the savepoint.
    #    Honest status codes drive the provider's retry behaviour.
    nested = db.begin_nested()
    try:
        billing_service.payment_provider_events.ingest(
            db,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                event_type=event_type,
                external_id=external_id or "",
                idempotency_key=idempotency_key,
                payload=payload,
            ),
        )
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

    # 3. Recorded as a PaymentProviderEvent — the insurance row is redundant.
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
