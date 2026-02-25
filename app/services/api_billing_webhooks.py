"""Billing API webhook orchestration."""

from __future__ import annotations

import json
import logging

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.models.billing import PaymentProviderType
from app.schemas.billing import PaymentProviderEventIngest
from app.services import billing as billing_service
from app.services.flutterwave import verify_webhook_signature as verify_flutterwave_signature
from app.services.paystack import verify_webhook_signature as verify_paystack_signature

_paystack_logger = logging.getLogger(__name__)
_flutterwave_logger = logging.getLogger(__name__)


def process_paystack_webhook(*, db: Session, body: bytes, signature: str) -> JSONResponse:
    if not verify_paystack_signature(body, signature, db):
        _paystack_logger.warning("Invalid Paystack webhook signature")
        return JSONResponse({"status": "invalid signature"}, status_code=400)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"status": "invalid JSON"}, status_code=400)

    event_type = payload.get("event", "unknown")
    data = payload.get("data", {})
    _paystack_logger.info("Paystack webhook: %s", event_type)

    provider = billing_service.payment_providers.get_by_type(db, PaymentProviderType.paystack)
    if not provider:
        _paystack_logger.warning("No Paystack payment provider configured, skipping ingest")
        return JSONResponse({"status": "ok"}, status_code=200)

    try:
        billing_service.payment_provider_events.ingest(
            db,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                event_type=event_type,
                external_id=str(data.get("id", "")),
                idempotency_key=f"paystack-{data.get('reference', data.get('id', ''))}",
                payload=payload,
            ),
        )
    except Exception as exc:
        _paystack_logger.error("Paystack webhook processing error: %s", exc)

    return JSONResponse({"status": "ok"}, status_code=200)


def process_flutterwave_webhook(*, db: Session, body: bytes, signature: str) -> JSONResponse:
    if not verify_flutterwave_signature(body, signature, db):
        _flutterwave_logger.warning("Invalid Flutterwave webhook signature")
        return JSONResponse({"status": "invalid signature"}, status_code=400)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"status": "invalid JSON"}, status_code=400)

    event_type = payload.get("event", "unknown")
    data = payload.get("data", {})
    _flutterwave_logger.info("Flutterwave webhook: %s", event_type)

    provider = billing_service.payment_providers.get_by_type(db, PaymentProviderType.flutterwave)
    if not provider:
        _flutterwave_logger.warning("No Flutterwave payment provider configured, skipping ingest")
        return JSONResponse({"status": "ok"}, status_code=200)

    try:
        billing_service.payment_provider_events.ingest(
            db,
            PaymentProviderEventIngest(
                provider_id=provider.id,
                event_type=event_type,
                external_id=str(data.get("id", "")),
                idempotency_key=f"flutterwave-{data.get('tx_ref', data.get('id', ''))}",
                payload=payload,
            ),
        )
    except Exception as exc:
        _flutterwave_logger.error("Flutterwave webhook processing error: %s", exc)

    return JSONResponse({"status": "ok"}, status_code=200)
