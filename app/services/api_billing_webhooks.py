"""Billing webhook adapter around the canonical integration inbox and owner."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Payment,
    PaymentProviderEvent,
    PaymentProviderType,
    PaymentStatus,
    TopupIntent,
)
from app.schemas.billing import PaymentProviderEventIngest
from app.services import billing as billing_service
from app.services.integrations import inbox as integration_inbox
from app.services.integrations import payment_capability
from app.services.integrations.inbox import InboxError
from app.services.integrations.installations import InstallationError
from app.services.topup_intents import set_topup_intent_status

logger = logging.getLogger(__name__)

_UNLINKED_SUCCESS_ERROR = "Successful settlement did not post or link a payment"

_PROVIDER_TYPE_BY_NAME = {
    "paystack": PaymentProviderType.paystack,
    "flutterwave": PaymentProviderType.flutterwave,
}


def _now() -> datetime:
    return datetime.now(UTC)


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
    # Gateway fee withheld from settlement (same currency as ``amount``). The
    # bank receives ``amount - fee``; ERP books the fee as a bank charge.
    fee: Decimal = Decimal("0.00")


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
        metadata = data.get("metadata")
        return _Settlement(
            status_hint=PaymentStatus.succeeded,
            amount=payment_capability.kobo_to_naira(data.get("amount") or 0),
            currency=str(data.get("currency") or "NGN"),
            reference=str(data.get("reference") or "") or None,
            metadata=metadata if isinstance(metadata, dict) else {},
            # Paystack reports its fee (in kobo) on the charge payload.
            fee=payment_capability.kobo_to_naira(data.get("fees") or 0),
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
                # Flutterwave reports its fee (in the charge currency) as app_fee.
                fee=Decimal(str(data.get("app_fee") or 0)),
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
    intent = db.get(TopupIntent, intent_id) if intent_id is not None else None
    if intent is None and settlement.reference:
        intent = db.scalar(
            select(TopupIntent).where(TopupIntent.reference == settlement.reference)
        )
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


def _prepare_provider_event_ingest(
    db: Session,
    *,
    provider_id,
    provider_type: str,
    event_type: str,
    external_id: str | None,
    idempotency_key: str | None,
    payload: dict,
) -> tuple[PaymentProviderEventIngest, _Settlement | None, TopupIntent | None]:
    """Build the canonical ingest command from a verified stored payload.

    Live delivery and dead-letter replay must use this exact projection.  A
    replay that forwards only the raw payload creates an idempotency event but
    omits the amount/account/status facts needed to post the payment.
    """
    data = payload.get("data", {}) or {}
    settlement = _extract_settlement(provider_type, event_type, data)
    topup_intent: TopupIntent | None = None
    ingest_payload = PaymentProviderEventIngest(
        provider_id=provider_id,
        event_type=event_type,
        external_id=external_id or "",
        idempotency_key=idempotency_key,
        payload=payload,
    )
    if settlement is None:
        return ingest_payload, None, None

    ingest_payload.status_hint = settlement.status_hint
    metadata = settlement.metadata or {}
    invoice_id = _coerce_uuid(metadata.get("invoice_id"))
    account_id = _coerce_uuid(metadata.get("account_id"))
    billing_account_id = _coerce_uuid(metadata.get("billing_account_id"))
    topup_intent = _resolve_settlement_topup_intent(db, settlement)
    if topup_intent is not None and topup_intent.account_id is not None:
        account_id = topup_intent.account_id
    if topup_intent is not None and topup_intent.billing_account_id is not None:
        billing_account_id = topup_intent.billing_account_id
    if (
        settlement.status_hint == PaymentStatus.succeeded
        and settlement.amount is not None
        and settlement.amount > Decimal("0.00")
    ):
        ingest_payload.amount = settlement.amount
        ingest_payload.provider_fee = settlement.fee
        ingest_payload.net_amount = (
            topup_intent.requested_amount
            if topup_intent is not None
            else settlement.amount - settlement.fee
        )
        ingest_payload.provider_reference = settlement.reference
        ingest_payload.topup_intent_id = (
            topup_intent.id if topup_intent is not None else None
        )
        ingest_payload.currency = settlement.currency
        ingest_payload.invoice_id = invoice_id
        ingest_payload.account_id = account_id
        ingest_payload.billing_account_id = billing_account_id
    return ingest_payload, settlement, topup_intent


def _apply_post_settlement_bookkeeping(
    db: Session,
    *,
    event,
    settlement: _Settlement | None,
    topup_intent: TopupIntent | None,
    ingest_payload: PaymentProviderEventIngest,
) -> None:
    """Run the recoverable consequences after the payment is durably posted."""
    if (
        settlement is None
        or settlement.status_hint != PaymentStatus.succeeded
        or event.payment_id is None
    ):
        return

    if settlement.fee and settlement.fee > Decimal("0.00"):
        try:
            pay = db.get(Payment, event.payment_id)
            if pay is not None and not pay.provider_fee:
                pay.provider_fee = settlement.fee
                db.commit()
        except Exception:
            logger.warning(
                "Failed to persist provider_fee after webhook settlement",
                exc_info=True,
            )
            db.rollback()
    if topup_intent is not None and topup_intent.purpose == "account_credit_deposit":
        # The deposit owner already linked intent/payment, applied any eligible
        # credit, and emitted its outbox event atomically. In particular, do not
        # run the legacy wallet/prepaid restore consequence below.
        return
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
    if account_for_restore is None:
        return
    try:
        from app.services import collections as collections_service
        from app.services.billing.reconcile_unposted import (
            settle_prepaid_draft_invoices_from_credit,
        )

        settled = settle_prepaid_draft_invoices_from_credit(
            db, str(account_for_restore)
        )
        if settled.changed:
            logger.info(
                "Settled %d prepaid draft invoice(s) after webhook top-up for %s",
                len(settled.invoices_settled),
                account_for_restore,
            )
            db.commit()
        collections_service.restore_account_services(db, str(account_for_restore))
    except Exception:
        logger.warning(
            "Failed to auto-restore after webhook settlement for %s",
            account_for_restore,
            exc_info=True,
        )
        db.rollback()


def _successful_settlement_is_unlinked(
    settlement: _Settlement | None, event: PaymentProviderEvent
) -> bool:
    """A provider success is incomplete until it identifies posted money."""
    return (
        settlement is not None
        and settlement.status_hint == PaymentStatus.succeeded
        and event.payment_id is None
    )


def _settle_typed_account_credit_deposit(
    db: Session,
    *,
    provider_type: str,
    external_id: str | None,
    settlement: _Settlement | None,
    topup_intent: TopupIntent | None,
    ingest_payload: PaymentProviderEventIngest,
) -> None:
    """Route a typed deposit webhook through the same owner as portal verify."""
    if (
        settlement is None
        or settlement.status_hint != PaymentStatus.succeeded
        or topup_intent is None
        or topup_intent.purpose != "account_credit_deposit"
    ):
        return
    if settlement.amount is None or not external_id:
        raise HTTPException(
            status_code=409,
            detail="Deposit provider confirmation omitted amount or transaction id",
        )
    from app.services.account_credit_deposits import (
        AccountCreditDeposits,
        DepositEligibilityError,
    )
    from app.services.payment_gateway_adapter import PaymentGatewayTransaction

    try:
        result = AccountCreditDeposits.settle_verified(
            db,
            intent_id=topup_intent.id,
            transaction=PaymentGatewayTransaction(
                provider_type=provider_type,
                external_id=external_id,
                amount=settlement.amount,
                currency=settlement.currency or topup_intent.currency,
                metadata=settlement.metadata or {},
                memo_prefix=provider_type.title(),
            ),
        )
    except DepositEligibilityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    ingest_payload.payment_id = result.payment.id


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
    ref_keys: Sequence[str],
) -> JSONResponse:
    try:
        binding, signature_valid = payment_capability.verify_webhook_signature(
            db,
            provider_type=provider_type,
            body=body,
            signature=signature,
        )
    except (InstallationError, payment_capability.PaymentCapabilityError) as exc:
        logger.error("%s webhook capability unavailable: %s", provider_type, exc)
        return JSONResponse({"status": "provider not configured"}, status_code=503)
    if not signature_valid:
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

    try:
        receipt, created = integration_inbox.receive_verified(
            db,
            capability_binding_id=binding.id,
            provider_event_id=idempotency_key,
            event_type=event_type,
            payload=payload,
            headers={"provider": provider_type},
        )
        db.commit()
        db.refresh(receipt)
    except InboxError as exc:
        db.commit()  # preserve a quarantine caused by identity collision
        logger.error("%s webhook identity rejected: %s", provider_type, exc)
        return JSONResponse({"status": "event identity conflict"}, status_code=409)

    if not created and receipt.state == "processed":
        consequence = dict(receipt.consequence_json or {})
        status_code = int(consequence.pop("http_status", 200))
        return JSONResponse(consequence or {"status": "ok"}, status_code=status_code)
    try:
        if not integration_inbox.claim_for_processing(receipt):
            return JSONResponse({"status": "ok"}, status_code=200)
        db.commit()
    except InboxError:
        return JSONResponse({"status": "event requires replay"}, status_code=500)

    provider = billing_service.payment_providers.get_by_type(
        db, _PROVIDER_TYPE_BY_NAME[provider_type]
    )
    if not provider:
        logger.warning(
            "No %s payment provider configured; webhook retained for replay",
            provider_type,
        )
        integration_inbox.mark_failed(
            receipt,
            error_code="payment_provider_not_configured",
            error_detail="No payment provider configured",
        )
        db.commit()
        return JSONResponse({"status": "provider not configured"}, status_code=503)

    # Project the verified payload through the same command builder manual
    # dead-letter replay uses. This is the single mapping from provider facts
    # to the payment-ingest owner.
    ingest_payload, settlement, topup_intent = _prepare_provider_event_ingest(
        db,
        provider_id=provider.id,
        provider_type=provider_type,
        event_type=event_type,
        external_id=external_id,
        idempotency_key=idempotency_key,
        payload=payload,
    )

    # Process inside a savepoint; the canonical inbox receipt was committed
    # before this point and remains the single retry/dead-letter record.
    nested = db.begin_nested()
    try:
        _settle_typed_account_credit_deposit(
            db,
            provider_type=provider_type,
            external_id=external_id,
            settlement=settlement,
            topup_intent=topup_intent,
            ingest_payload=ingest_payload,
        )
        event = billing_service.payment_provider_events.ingest(
            db,
            ingest_payload,
            trusted_financial_effects=True,
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
        integration_inbox.mark_failed(
            receipt,
            error_code="payment_event_rejected",
            error_detail=f"{exc.status_code}: {exc.detail}",
            max_attempts=1,
        )
        db.commit()
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
        integration_inbox.mark_failed(
            receipt,
            error_code="payment_event_processing_failed",
            error_detail=str(exc),
        )
        db.commit()
        return JSONResponse({"status": "error"}, status_code=500)

    if _successful_settlement_is_unlinked(settlement, event):
        integration_inbox.mark_failed(
            receipt,
            error_code="payment_settlement_unlinked",
            error_detail=_UNLINKED_SUCCESS_ERROR,
        )
        db.commit()
        return JSONResponse({"status": "error"}, status_code=500)

    # Best-effort consequences after the money is durably recorded.
    _apply_post_settlement_bookkeeping(
        db,
        event=event,
        settlement=settlement,
        topup_intent=topup_intent,
        ingest_payload=ingest_payload,
    )

    integration_inbox.mark_processed(
        receipt,
        consequence={"status": "ok", "http_status": 200},
    )
    db.commit()
    return JSONResponse({"status": "ok"}, status_code=200)


def process_paystack_webhook(
    *, db: Session, body: bytes, signature: str
) -> JSONResponse:
    return _process_webhook(
        db=db,
        body=body,
        signature=signature,
        provider_type="paystack",
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
        ref_keys=("tx_ref",),
    )
