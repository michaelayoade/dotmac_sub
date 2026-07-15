"""Owner for consolidated billing-account payment settlement.

This owner keeps reseller-held credit separate from subscriber ledger state.
It previews the exact FIFO or explicit invoice allocations, rechecks them while
holding the billing-account lock, and links every resulting transaction to one
``PaymentSettlement``. Routes, provider adapters, proof approval, and
reconciliation call this owner; they do not construct settled money state.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import (
    BillingAccount,
    BillingAccountLedgerEntry,
    Invoice,
    InvoiceStatus,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    BillingAccountPaymentAllocationEffectRead,
    BillingAccountPaymentConfirm,
    BillingAccountPaymentPreviewRead,
    BillingAccountPaymentPreviewRequest,
)
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _assert_invoice_allocatable,
    _resolve_collection_account,
    _resolve_payment_channel,
    _validate_collection_account,
    _validate_payment_provider,
)
from app.services.billing.payments import (
    _apply_payment_allocation,
    _emit_consolidated_payment_events,
    _finalize_invoice_payment_effects,
)
from app.services.common import get_by_id, round_money, to_decimal

_IDEMPOTENCY_SCOPE = "consolidated_payment_settlement"
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{15,119}")
_OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def consolidated_settlement_key(namespace: str, source_id: str) -> str:
    """Return a stable, safe key without leaking arbitrary provider text."""
    digest = hashlib.sha256(f"{namespace}:{source_id}".encode()).hexdigest()
    return f"consolidated-{namespace}-{digest}"


@dataclass(frozen=True)
class ConsolidatedPaymentSettlementResult:
    payment: Payment
    settlement: PaymentSettlement
    preview: BillingAccountPaymentPreviewRead | None
    idempotent_replay: bool = False


def _normalize_key(value: str) -> str:
    key = value.strip()
    if not _SAFE_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail=(
                "Consolidated payment idempotency key must be 16-120 safe characters"
            ),
        )
    return key


def _lock_billing_account(db: Session, billing_account_id) -> BillingAccount:
    account = (
        db.query(BillingAccount)
        .filter(BillingAccount.id == billing_account_id)
        .with_for_update()
        .first()
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Billing account not found")
    if not account.is_active or account.status != "active":
        raise HTTPException(status_code=409, detail="Billing account is not active")
    return account


def _validate_currency(account: BillingAccount, currency: str) -> str:
    normalized = currency.strip().upper()
    if normalized != account.currency.upper():
        raise HTTPException(
            status_code=409,
            detail="Payment currency must match the consolidated billing account",
        )
    return normalized


def _candidate_invoices(
    db: Session,
    account: BillingAccount,
    request: BillingAccountPaymentPreviewRequest,
) -> list[tuple[Invoice, Decimal]]:
    remaining = round_money(to_decimal(request.amount))
    if request.allocations:
        candidates: list[tuple[Invoice, Decimal]] = []
        seen: set = set()
        for requested in request.allocations:
            if requested.invoice_id in seen:
                raise HTTPException(
                    status_code=400,
                    detail="A consolidated payment can name each invoice only once",
                )
            seen.add(requested.invoice_id)
            invoice = get_by_id(db, Invoice, requested.invoice_id)
            if invoice is None:
                raise HTTPException(status_code=404, detail="Invoice not found")
            subscriber = get_by_id(db, Subscriber, invoice.account_id)
            if subscriber is None or subscriber.reseller_id != account.reseller_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invoice does not belong to a subscriber of this billing "
                        "account's reseller"
                    ),
                )
            if invoice.currency.upper() != request.currency.upper():
                raise HTTPException(
                    status_code=400,
                    detail="Invoice currency does not match payment currency",
                )
            _assert_invoice_allocatable(invoice)
            requested_amount = round_money(to_decimal(requested.amount))
            if requested_amount > remaining:
                raise HTTPException(
                    status_code=400,
                    detail="Allocation amount exceeds payment amount",
                )
            candidates.append((invoice, requested_amount))
            remaining = round_money(remaining - requested_amount)
        return candidates
    if not request.auto_allocate:
        return []
    return [
        (invoice, round_money(to_decimal(invoice.balance_due)))
        for invoice in (
            db.query(Invoice)
            .join(Subscriber, Invoice.account_id == Subscriber.id)
            .filter(Subscriber.reseller_id == account.reseller_id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .filter(Invoice.balance_due > Decimal("0.00"))
            .filter(Invoice.currency == request.currency)
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
    ]


def _fingerprint_payload(
    request: BillingAccountPaymentPreviewRequest,
    preview_values: dict,
) -> str:
    request_values = request.model_dump(mode="json")
    canonical = json.dumps(
        {"request": request_values, **preview_values},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class ConsolidatedPaymentSettlements:
    """Single writer for confirmed consolidated payment money effects."""

    @staticmethod
    def preview(
        db: Session,
        billing_account_id: str,
        request: BillingAccountPaymentPreviewRequest,
    ) -> BillingAccountPaymentPreviewRead:
        account = get_by_id(db, BillingAccount, billing_account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Billing account not found")
        if not account.is_active or account.status != "active":
            raise HTTPException(status_code=409, detail="Billing account is not active")
        currency = _validate_currency(account, request.currency)
        amount = round_money(to_decimal(request.amount))
        remaining = amount
        effects: list[BillingAccountPaymentAllocationEffectRead] = []
        for invoice, requested_amount in _candidate_invoices(db, account, request):
            before = max(Decimal("0.00"), round_money(to_decimal(invoice.balance_due)))
            applied = min(remaining, requested_amount, before)
            if applied <= Decimal("0.00"):
                continue
            effects.append(
                BillingAccountPaymentAllocationEffectRead(
                    invoice_id=invoice.id,
                    account_id=invoice.account_id,
                    invoice_number=invoice.invoice_number,
                    receivable_before=before,
                    receivable_after=round_money(before - applied),
                    allocation_amount=applied,
                    ledger_entry_type=LedgerEntryType.credit,
                    ledger_source=LedgerSource.payment,
                )
            )
            remaining = round_money(remaining - applied)
            if remaining <= Decimal("0.00"):
                break
        consolidated_before = round_money(to_decimal(account.balance))
        allocated = round_money(amount - remaining)
        preview_values: dict[str, object] = {
            "billing_account_id": str(account.id),
            "payment_state": PaymentStatus.succeeded.value,
            "consolidated_credit_before": str(consolidated_before),
            "consolidated_credit_after": str(
                round_money(consolidated_before + remaining)
            ),
            "allocation_effects": [
                effect.model_dump(mode="json") for effect in effects
            ],
            "allocated_amount": str(allocated),
            "unallocated_amount": str(remaining),
            "payment_consequence": "confirmed_consolidated_payment_settlement",
            "service_access_consequence": (
                "request_reconciliation_for_paid_member_invoices_no_direct_access_decision"
            ),
        }
        return BillingAccountPaymentPreviewRead(
            billing_account_id=account.id,
            amount=amount,
            currency=currency,
            payment_state=PaymentStatus.succeeded,
            consolidated_credit_before=consolidated_before,
            consolidated_credit_after=round_money(consolidated_before + remaining),
            allocation_effects=effects,
            allocated_amount=allocated,
            unallocated_amount=remaining,
            unallocated_ledger_entry_type=(
                LedgerEntryType.credit if remaining > Decimal("0.00") else None
            ),
            unallocated_ledger_source=(
                LedgerSource.payment if remaining > Decimal("0.00") else None
            ),
            payment_consequence=str(preview_values["payment_consequence"]),
            service_access_consequence=str(
                preview_values["service_access_consequence"]
            ),
            fingerprint=_fingerprint_payload(request, preview_values),
        )

    @staticmethod
    def _replay(
        db: Session, *, key: str, fingerprint: str
    ) -> ConsolidatedPaymentSettlementResult | None:
        reservation = (
            db.query(IdempotencyKey)
            .filter(IdempotencyKey.scope == _IDEMPOTENCY_SCOPE)
            .filter(IdempotencyKey.key == key)
            .first()
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Consolidated payment is being recorded"
            )
        payment = get_by_id(db, Payment, reservation.ref_id)
        if payment is None or payment.settlement is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payment settlement evidence is incomplete",
            )
        if payment.creation_preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different payment preview",
            )
        return ConsolidatedPaymentSettlementResult(
            payment=payment,
            settlement=payment.settlement,
            preview=None,
            idempotent_replay=True,
        )

    @classmethod
    def settle_verified(
        cls,
        db: Session,
        billing_account_id: str,
        request: BillingAccountPaymentPreviewRequest,
        *,
        idempotency_key: str,
        origin: PaymentSettlementOrigin,
        actor_id: str | None = None,
        commit: bool = True,
        existing_payment_id: str | None = None,
    ) -> ConsolidatedPaymentSettlementResult:
        """Confirm a provider/operator fact through the same preview contract.

        The caller's verification or approval is the confirmation boundary;
        this helper still materializes the owner preview and binds the command
        to its fingerprint before any money is written.
        """
        preview = cls.preview(db, billing_account_id, request)
        command = BillingAccountPaymentConfirm(
            **request.model_dump(),
            preview_fingerprint=preview.fingerprint,
            idempotency_key=idempotency_key,
        )
        return cls.confirm(
            db,
            billing_account_id,
            command,
            origin=origin,
            actor_id=actor_id,
            commit=commit,
            existing_payment_id=existing_payment_id,
        )

    @classmethod
    def confirm(
        cls,
        db: Session,
        billing_account_id: str,
        command: BillingAccountPaymentConfirm,
        *,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.manual,
        actor_id: str | None = None,
        commit: bool = True,
        existing_payment_id: str | None = None,
    ) -> ConsolidatedPaymentSettlementResult:
        key = _normalize_key(command.idempotency_key)
        replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
        if replay is not None:
            return replay
        account = _lock_billing_account(db, billing_account_id)
        request = BillingAccountPaymentPreviewRequest(
            **command.model_dump(exclude={"preview_fingerprint", "idempotency_key"})
        )
        preview = cls.preview(db, str(account.id), request)
        if preview.fingerprint != command.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
        if replay is not None:
            return replay
        reservation = IdempotencyKey(scope=_IDEMPOTENCY_SCOPE, key=key)
        db.add(reservation)
        try:
            _validate_payment_provider(
                db, str(command.provider_id) if command.provider_id else None
            )
            channel = _resolve_payment_channel(
                db,
                str(command.payment_channel_id) if command.payment_channel_id else None,
                str(command.payment_method_id) if command.payment_method_id else None,
                str(command.provider_id) if command.provider_id else None,
            )
            collection_account = _resolve_collection_account(
                db,
                channel,
                preview.currency,
                str(command.collection_account_id)
                if command.collection_account_id
                else None,
            )
            if command.collection_account_id and collection_account is None:
                _validate_collection_account(
                    db, str(command.collection_account_id), preview.currency
                )
            resolved_channel_id = command.payment_channel_id or (
                channel.id if channel is not None else None
            )
            resolved_collection_account_id = command.collection_account_id or (
                collection_account.id if collection_account is not None else None
            )
            if existing_payment_id is not None:
                payment = (
                    db.query(Payment)
                    .filter(Payment.id == existing_payment_id)
                    .with_for_update()
                    .first()
                )
                if payment is None:
                    raise HTTPException(status_code=404, detail="Payment not found")
                if payment.billing_account_id != account.id or payment.account_id:
                    raise HTTPException(
                        status_code=409,
                        detail="Payment does not belong to this billing account",
                    )
                if payment.settlement is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Payment already has different settlement evidence",
                    )
                if payment.status != PaymentStatus.pending:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Only a no-money pending observation can be confirmed; "
                            "historical succeeded rows require reconciliation"
                        ),
                    )
                if (
                    round_money(to_decimal(payment.amount)) != preview.amount
                    or payment.currency != preview.currency
                    or payment.provider_id != command.provider_id
                    or payment.external_id != command.external_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Confirmed payment no longer matches its observation",
                    )
                payment.status = PaymentStatus.succeeded
                payment.paid_at = command.paid_at or datetime.now(UTC)
                payment.payment_method_id = command.payment_method_id
                payment.payment_channel_id = resolved_channel_id
                payment.collection_account_id = resolved_collection_account_id
                payment.auto_allocate_on_settlement = command.auto_allocate
                payment.creation_preview_fingerprint = preview.fingerprint
                payment.memo = command.memo
            else:
                payment = Payment(
                    billing_account_id=account.id,
                    payment_method_id=command.payment_method_id,
                    payment_channel_id=resolved_channel_id,
                    collection_account_id=resolved_collection_account_id,
                    provider_id=command.provider_id,
                    amount=preview.amount,
                    currency=preview.currency,
                    status=PaymentStatus.succeeded,
                    paid_at=command.paid_at or datetime.now(UTC),
                    auto_allocate_on_settlement=command.auto_allocate,
                    creation_preview_fingerprint=preview.fingerprint,
                    external_id=command.external_id,
                    memo=command.memo,
                )
                db.add(payment)
            db.flush()
            allocations: list[PaymentAllocation] = []
            for effect in preview.allocation_effects:
                invoice = get_by_id(db, Invoice, effect.invoice_id)
                if invoice is None:
                    raise HTTPException(status_code=404, detail="Invoice not found")
                allocation, applied = _apply_payment_allocation(
                    db, payment, invoice, effect.allocation_amount
                )
                if applied != effect.allocation_amount:
                    raise HTTPException(
                        status_code=409,
                        detail="Consolidated allocation result no longer matches preview",
                    )
                allocation.preview_fingerprint = preview.fingerprint
                allocations.append(allocation)
            billing_entry: BillingAccountLedgerEntry | None = None
            if preview.unallocated_amount > Decimal("0.00"):
                account.balance = round_money(
                    to_decimal(account.balance) + preview.unallocated_amount
                )
                account.updated_at = datetime.now(UTC)
                billing_entry = BillingAccountLedgerEntry(
                    billing_account_id=account.id,
                    payment_id=payment.id,
                    entry_type=LedgerEntryType.credit,
                    source=LedgerSource.payment,
                    amount=preview.unallocated_amount,
                    currency=preview.currency,
                    balance_after=account.balance,
                    memo="Unallocated consolidated payment credit",
                )
                db.add(billing_entry)
            db.flush()
            for allocation in allocations:
                invoice = get_by_id(db, Invoice, allocation.invoice_id)
                if invoice is not None:
                    _finalize_invoice_payment_effects(db, invoice)
            settlement = PaymentSettlement(
                payment_id=payment.id,
                billing_account_ledger_entry_id=(
                    billing_entry.id if billing_entry is not None else None
                ),
                amount=preview.amount,
                unallocated_amount=preview.unallocated_amount,
                prepaid_amount=Decimal("0.00"),
                currency=preview.currency,
                origin=origin,
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(settlement)
            db.flush()
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=(
                        AuditActorType.user if actor_id else AuditActorType.system
                    ),
                    actor_id=actor_id,
                    action="settle_consolidated_payment",
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata_={
                        "billing_account_id": str(account.id),
                        "settlement_id": str(settlement.id),
                        "amount": str(preview.amount),
                        "currency": preview.currency,
                        "origin": origin.value,
                        "preview_fingerprint": preview.fingerprint,
                        "allocation_ledger_entry_ids": [
                            str(allocation.ledger_entry_id)
                            for allocation in allocations
                            if allocation.ledger_entry_id is not None
                        ],
                        "billing_account_ledger_entry_id": (
                            str(billing_entry.id) if billing_entry is not None else None
                        ),
                        "allocated_amount": str(preview.allocated_amount),
                        "unallocated_amount": str(preview.unallocated_amount),
                        "consolidated_credit_before": str(
                            preview.consolidated_credit_before
                        ),
                        "consolidated_credit_after": str(
                            preview.consolidated_credit_after
                        ),
                        "payment_consequence": preview.payment_consequence,
                        "service_access_consequence": (
                            preview.service_access_consequence
                        ),
                    },
                ),
            )
            reservation.ref_id = str(payment.id)
            _emit_consolidated_payment_events(db, payment, allocations)
            db.flush()
            if commit:
                db.commit()
                db.refresh(payment)
                db.refresh(settlement)
            return ConsolidatedPaymentSettlementResult(
                payment=payment,
                settlement=settlement,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = cls._replay(db, key=key, fingerprint=command.preview_fingerprint)
            if replay is not None:
                return replay
            raise HTTPException(
                status_code=409, detail="Consolidated payment is already recorded"
            ) from exc
        except Exception:
            db.rollback()
            raise


consolidated_payment_settlements = ConsolidatedPaymentSettlements()
