"""Source-of-truth state machine for VAS refund-to-source operations.

The wallet reservation and request are committed before a gateway call. A
provider response is only an observation: this service persists it, projects
terminal failure as an explicit wallet reversal, and safely resumes every
non-terminal request from the same durable row.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import TopupIntent
from app.models.vas import (
    VasEntryCategory,
    VasEntryType,
    VasRefundRequest,
    VasRefundStatus,
    VasWalletEntry,
)
from app.services import vas_wallet
from app.services.payment_gateway_adapter import (
    PaymentGatewayRefund,
    PaymentGatewayRefundState,
    payment_gateway_adapter,
)

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = frozenset({"paystack", "flutterwave"})
RECONCILE_ATTENTION_THRESHOLD = 3


class VasRefundError(ValueError):
    """A refund rejection safe to render to an administrator."""


@dataclass(frozen=True)
class VasRefundOutcome:
    request_id: str
    entry_id: str
    provider: str
    reference: str
    amount: Decimal
    status: VasRefundStatus
    already_requested: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _outcome(
    request: VasRefundRequest, *, already_requested: bool = False
) -> VasRefundOutcome:
    return VasRefundOutcome(
        request_id=str(request.id),
        entry_id=str(request.topup_entry_id),
        provider=request.provider,
        reference=request.funding_reference,
        amount=Decimal(str(request.amount)),
        status=request.status,
        already_requested=already_requested,
    )


def _request_for_entry(db: Session, entry_id: uuid.UUID) -> VasRefundRequest | None:
    return db.scalars(
        select(VasRefundRequest).where(VasRefundRequest.topup_entry_id == entry_id)
    ).first()


def _gateway_intent(db: Session, reference: str) -> TopupIntent | None:
    return db.scalars(
        select(TopupIntent).where(TopupIntent.reference == reference)
    ).first()


def _prepare_request(db: Session, *, entry_id: str) -> tuple[VasRefundRequest, bool]:
    entry = vas_wallet.topup_entry(db, entry_id)
    if (
        entry is None
        or entry.category != VasEntryCategory.topup
        or entry.entry_type != VasEntryType.credit
        or not entry.reference
    ):
        raise VasRefundError("Entry is not a refundable top-up")

    existing = _request_for_entry(db, entry.id)
    if existing is not None:
        return existing, True

    wallet = vas_wallet.wallet_by_id(db, entry.wallet_id)
    if wallet is None:
        raise VasRefundError("Wallet not found")

    gateway_intent = _gateway_intent(db, entry.reference)
    provider = (
        str(gateway_intent.provider_type).strip().lower()
        if gateway_intent is not None
        else vas_wallet.funding_provider_for_entry(db, entry)
    )
    if provider not in SUPPORTED_PROVIDERS:
        raise VasRefundError("The top-up funding provider does not support refunds")

    amount = Decimal(str(entry.amount))
    request = VasRefundRequest(
        topup_entry_id=entry.id,
        wallet_id=wallet.id,
        provider=provider,
        funding_reference=entry.reference,
        provider_transaction_id=(
            str(gateway_intent.external_id).strip()
            if gateway_intent is not None and gateway_intent.external_id
            else None
        ),
        amount=amount,
        currency=str(entry.currency or "NGN").upper(),
        status=VasRefundStatus.prepared,
    )
    db.add(request)
    try:
        db.flush()
        debit = vas_wallet.debit_wallet(
            db,
            wallet,
            amount=amount,
            category=VasEntryCategory.adjustment,
            reference=f"rts-{entry.id}",
            memo=f"Refund to source ({entry.reference}); request {request.id}",
            commit=False,
        )
        request.wallet_debit_entry_id = debit.id
        db.commit()
        db.refresh(request)
        return request, False
    except IntegrityError as exc:
        db.rollback()
        raced = _request_for_entry(db, entry.id)
        if raced is not None:
            return raced, True
        raise VasRefundError("This top-up was already refunded") from exc
    except HTTPException as exc:
        db.rollback()
        raise VasRefundError("Wallet balance is below the top-up amount") from exc
    except Exception:
        db.rollback()
        raise


def _record_error(
    db: Session,
    request_id: uuid.UUID,
    exc: Exception,
    *,
    reconciliation: bool,
) -> VasRefundRequest:
    # Gateway adapters only read settings from this session. Close that
    # read-only transaction without rolling back the already-committed durable
    # request (important for both production crash recovery and nested tests).
    db.commit()
    request = db.get(VasRefundRequest, request_id)
    if request is None:
        raise RuntimeError(f"VAS refund request {request_id} disappeared") from exc
    request.last_error = str(exc)[:2000]
    if reconciliation:
        request.reconcile_attempts += 1
        request.last_reconciled_at = _now()
        if request.reconcile_attempts >= RECONCILE_ATTENTION_THRESHOLD:
            request.status = VasRefundStatus.needs_attention
    db.commit()
    db.refresh(request)
    logger.warning(
        "vas_refund_gateway_observation_failed",
        extra={
            "event": "vas_refund_gateway_observation_failed",
            "request_id": str(request.id),
            "provider": request.provider,
            "status": request.status.value,
            "error": str(exc),
        },
    )
    return request


def _resolve_transaction(db: Session, request: VasRefundRequest) -> VasRefundRequest:
    if request.provider_transaction_id:
        return request
    try:
        transaction = payment_gateway_adapter.verify(
            db,
            provider_type=request.provider,
            reference=request.funding_reference,
        )
        if transaction.amount != Decimal(str(request.amount)):
            raise ValueError(
                "Gateway transaction amount does not match the wallet top-up"
            )
        if transaction.currency.upper() != request.currency.upper():
            raise ValueError(
                "Gateway transaction currency does not match the wallet top-up"
            )
        request.provider_transaction_id = transaction.external_id
        request.last_error = None
        db.commit()
        db.refresh(request)
        return request
    except Exception as exc:
        return _record_error(db, request.id, exc, reconciliation=False)


def _wallet_entry_by_reference(db: Session, reference: str) -> VasWalletEntry | None:
    return db.scalars(
        select(VasWalletEntry).where(VasWalletEntry.reference == reference)
    ).first()


def _apply_observation(
    db: Session,
    request: VasRefundRequest,
    observation: PaymentGatewayRefund,
) -> VasRefundRequest:
    request.provider_refund_id = observation.external_id or request.provider_refund_id
    request.provider_status = observation.status
    request.provider_response = observation.raw
    request.last_error = None
    request.last_reconciled_at = _now()

    observed_amount = Decimal(str(observation.amount))
    if observed_amount and observed_amount != Decimal(str(request.amount)):
        request.status = VasRefundStatus.needs_attention
        request.last_error = (
            f"Provider refund amount {observed_amount} does not match "
            f"request amount {request.amount}"
        )
    elif observation.state == PaymentGatewayRefundState.succeeded:
        request.status = VasRefundStatus.succeeded
        request.completed_at = _now()
    elif observation.state == PaymentGatewayRefundState.failed:
        reversal_reference = f"rts-reversal-{request.id}"
        reversal = (
            db.get(VasWalletEntry, request.wallet_reversal_entry_id)
            if request.wallet_reversal_entry_id
            else _wallet_entry_by_reference(db, reversal_reference)
        )
        if reversal is None:
            wallet = vas_wallet.wallet_by_id(db, request.wallet_id)
            if wallet is None:
                raise RuntimeError("Refund request wallet no longer exists")
            reversal = vas_wallet.credit_wallet(
                db,
                wallet,
                amount=Decimal(str(request.amount)),
                category=VasEntryCategory.adjustment,
                reference=reversal_reference,
                memo=(
                    f"Refund-to-source gateway failure reversal; request {request.id}"
                ),
                commit=False,
            )
        request.wallet_reversal_entry_id = reversal.id
        request.status = VasRefundStatus.failed
        request.completed_at = _now()
    elif observation.state == PaymentGatewayRefundState.needs_attention:
        request.status = VasRefundStatus.needs_attention
    else:
        request.status = VasRefundStatus.accepted

    db.commit()
    db.refresh(request)
    return request


def _submit_prepared(db: Session, request: VasRefundRequest) -> VasRefundRequest:
    request = _resolve_transaction(db, request)
    if not request.provider_transaction_id:
        return request

    request.status = VasRefundStatus.submitting
    request.submit_attempts += 1
    request.submitted_at = _now()
    request.last_error = None
    db.commit()
    db.refresh(request)

    try:
        observation = payment_gateway_adapter.refund(
            db,
            provider_type=request.provider,
            reference=request.funding_reference,
            transaction_id=request.provider_transaction_id,
            amount=Decimal(str(request.amount)),
            request_key=str(request.id),
        )
    except Exception as exc:
        # The external call may have succeeded even when the response was lost.
        # Keep the wallet reservation and let observation-only reconciliation
        # discover the provider result; never issue a blind second refund.
        return _record_error(db, request.id, exc, reconciliation=False)
    return _apply_observation(db, request, observation)


def _observe_request(db: Session, request: VasRefundRequest) -> VasRefundRequest:
    if not request.provider_transaction_id:
        return _resolve_transaction(db, request)
    try:
        observation = payment_gateway_adapter.find_refund(
            db,
            provider_type=request.provider,
            transaction_id=request.provider_transaction_id,
            request_key=str(request.id),
            refund_id=request.provider_refund_id,
        )
    except Exception as exc:
        return _record_error(db, request.id, exc, reconciliation=True)
    request.reconcile_attempts += 1
    request.last_reconciled_at = _now()
    if observation is None:
        if request.reconcile_attempts >= RECONCILE_ATTENTION_THRESHOLD:
            request.status = VasRefundStatus.needs_attention
            request.last_error = "Provider has not exposed the submitted refund"
        db.commit()
        db.refresh(request)
        return request
    return _apply_observation(db, request, observation)


def request_refund(db: Session, *, entry_id: str) -> VasRefundOutcome:
    """Reserve wallet funds durably and initiate one source refund."""
    request, already_requested = _prepare_request(db, entry_id=entry_id)
    if request.status == VasRefundStatus.prepared:
        request = _submit_prepared(db, request)
    elif request.status in {
        VasRefundStatus.submitting,
        VasRefundStatus.accepted,
        VasRefundStatus.needs_attention,
    }:
        request = _observe_request(db, request)

    logger.info(
        "vas_refund_request",
        extra={
            "event": "vas_refund_request",
            "request_id": str(request.id),
            "entry_id": str(request.topup_entry_id),
            "provider": request.provider,
            "status": request.status.value,
            "already_requested": already_requested,
        },
    )
    return _outcome(request, already_requested=already_requested)


def reconcile_refund_requests(db: Session, *, limit: int = 100) -> dict[str, int]:
    """Advance every non-terminal refund from provider observations."""
    requests = list(
        db.scalars(
            select(VasRefundRequest)
            .where(
                VasRefundRequest.status.in_(
                    (
                        VasRefundStatus.prepared,
                        VasRefundStatus.submitting,
                        VasRefundStatus.accepted,
                        VasRefundStatus.needs_attention,
                    )
                )
            )
            .order_by(VasRefundRequest.updated_at.asc())
            .limit(max(1, min(limit, 500)))
        ).all()
    )
    stats = {
        "checked": 0,
        "accepted": 0,
        "succeeded": 0,
        "failed": 0,
        "needs_attention": 0,
    }
    for item in requests:
        stats["checked"] += 1
        if item.status == VasRefundStatus.prepared:
            result = _submit_prepared(db, item)
        else:
            result = _observe_request(db, item)
        if result.status.value in stats:
            stats[result.status.value] += 1
    return stats


def admin_refund_requests(db: Session, *, limit: int = 50) -> list[VasRefundRequest]:
    return list(
        db.scalars(
            select(VasRefundRequest)
            .order_by(VasRefundRequest.created_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
    )
