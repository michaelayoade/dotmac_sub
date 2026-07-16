"""Payment and payment method management services."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditActorType
from app.models.billing import (
    BankAccount,
    BankAccountType,
    CollectionAccount,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentMethod,
    PaymentMethodType,
    PaymentProviderEvent,
    PaymentProviderEventFinancialEffect,
    PaymentRefund,
    PaymentRefundOrigin,
    PaymentReversal,
    PaymentReversalOrigin,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    TaxApplication,
    TaxRate,
)
from app.models.catalog import (
    BillingCycle,
    BillingMode,
    CatalogOffer,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import SettingDomain
from app.models.idempotency import IdempotencyKey
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    BankAccountCreate,
    BankAccountUpdate,
    CollectionAccountCreate,
    CollectionAccountUpdate,
    LedgerEntryCreate,
    PaymentAllocationApply,
    PaymentAllocationConfirm,
    PaymentAllocationCreate,
    PaymentAllocationPreviewRequest,
    PaymentChannelAccountCreate,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelUpdate,
    PaymentCreate,
    PaymentCreationConfirm,
    PaymentCreationPreviewRequest,
    PaymentMethodCreate,
    PaymentMethodUpdate,
    PaymentRefundPreviewRequest,
    PaymentRefundRequest,
    PaymentReversalPreviewRequest,
    PaymentReversalRequest,
    PaymentSettlementReconciliationRequest,
    PaymentUpdate,
)
from app.services import settings_spec
from app.services.audit import AuditEvents
from app.services.billing._common import (
    _assert_invoice_allocatable,
    _recalculate_invoice_totals,
    _resolve_collection_account,
    _resolve_payment_channel,
    _validate_account,
    _validate_collection_account,
    _validate_invoice_currency,
    _validate_payment_channel,
    _validate_payment_linkages,
    _validate_payment_provider,
    get_account_credit_balance,
    lock_account,
)
from app.services.billing.ledger import LedgerEntries
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    round_money,
    to_decimal,
    validate_enum,
)
from app.services.credential_crypto import decrypt_credential, encrypt_credential
from app.services.customer_financial_ledger import calculate_customer_balance
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.response import ListResponseMixin
from app.services.service_entitlements import (
    ensure_prepaid_entitlement_for_wallet_debit,
    ensure_prepaid_entitlements_for_paid_invoice,
    revoke_prepaid_entitlements_for_unpaid_invoice,
)
from app.services.sync_feeds import apply_sync_page, sync_page_response

logger = logging.getLogger(__name__)

_REFUND_IDEMPOTENCY_SCOPE = "payment_refund"
_REFUND_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,120}$")
_REFUND_CONSUMPTION_MEMO_PREFIX = "Payment refund account-credit consumption:"
_REVERSAL_IDEMPOTENCY_SCOPE = "payment_reversal"
_REVERSAL_CONSUMPTION_MEMO_PREFIX = "Payment reversal account-credit consumption:"
_PAYMENT_CREATION_IDEMPOTENCY_SCOPE = "payment_creation"
_PAYMENT_ALLOCATION_IDEMPOTENCY_SCOPE = "payment_allocation"
_PAYMENT_ALLOCATION_CONSUMPTION_MEMO_PREFIX = (
    "Payment allocation account-credit consumption:"
)


@dataclass(frozen=True)
class RefundCapability:
    allowed: bool
    reason: str | None


@dataclass(frozen=True)
class PaymentEditCapability:
    allowed: bool
    reason: str | None


@dataclass(frozen=True)
class PaymentCreationAllocationEffect:
    invoice_id: UUID
    invoice_number: str | None
    receivable_before: Decimal
    receivable_after: Decimal
    allocation_amount: Decimal
    ledger_entry_type: LedgerEntryType = LedgerEntryType.credit
    ledger_source: LedgerSource = LedgerSource.payment


@dataclass(frozen=True)
class PaymentPrepaidServiceEffect:
    subscription_id: UUID
    charge_amount: Decimal
    period_start: datetime
    period_end: datetime
    ledger_entry_type: LedgerEntryType | None
    ledger_source: LedgerSource | None
    consequence: str


@dataclass(frozen=True)
class PaymentCreationPreview:
    account_id: UUID
    amount: Decimal
    currency: str
    status: PaymentStatus
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    allocation_effects: tuple[PaymentCreationAllocationEffect, ...]
    unallocated_amount: Decimal
    unallocated_ledger_entry_type: LedgerEntryType | None
    unallocated_ledger_source: LedgerSource | None
    prepaid_service_effect: PaymentPrepaidServiceEffect | None
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class PaymentCreationResult:
    payment: Payment
    settlement: PaymentSettlement | None
    preview: PaymentCreationPreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "payment_id": str(self.payment.id),
            "settlement_id": str(self.settlement.id) if self.settlement else None,
            "amount": str(self.payment.amount),
            "currency": self.payment.currency,
            "status": self.payment.status.value,
            "preview_fingerprint": (
                self.preview.fingerprint
                if self.preview
                else (self.settlement.preview_fingerprint if self.settlement else None)
            ),
            "allocation_ledger_entry_ids": [
                str(allocation.ledger_entry_id)
                for allocation in self.payment.allocations
                if allocation.ledger_entry_id is not None
            ],
            "unallocated_ledger_entry_id": (
                str(self.settlement.unallocated_ledger_entry_id)
                if self.settlement and self.settlement.unallocated_ledger_entry_id
                else None
            ),
            "prepaid_ledger_entry_id": (
                str(self.settlement.prepaid_ledger_entry_id)
                if self.settlement and self.settlement.prepaid_ledger_entry_id
                else None
            ),
            "prepaid_amount": (
                str(self.settlement.prepaid_amount) if self.settlement else "0.00"
            ),
            "access_consequence": (
                self.preview.access_consequence
                if self.preview
                else (
                    "recheck_after_payment_settlement"
                    if self.settlement
                    else "none_until_payment_settlement"
                )
            ),
        }


@dataclass(frozen=True)
class PaymentAllocationPreview:
    payment_id: UUID
    settlement_id: UUID
    invoice_id: UUID
    invoice_number: str | None
    amount: Decimal
    currency: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    payment_unallocated_before: Decimal
    payment_unallocated_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    receivable_before: Decimal
    receivable_after: Decimal
    invoice_ledger_entry_type: LedgerEntryType
    invoice_ledger_source: LedgerSource
    account_credit_ledger_entry_type: LedgerEntryType
    account_credit_ledger_source: LedgerSource
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class PaymentAllocationResult:
    allocation: PaymentAllocation
    preview: PaymentAllocationPreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "allocation_id": str(self.allocation.id),
            "payment_id": str(self.allocation.payment_id),
            "invoice_id": str(self.allocation.invoice_id),
            "amount": str(self.allocation.amount),
            "preview_fingerprint": (
                self.preview.fingerprint
                if self.preview
                else self.allocation.preview_fingerprint
            ),
            "invoice_ledger_entry_id": (
                str(self.allocation.ledger_entry_id)
                if self.allocation.ledger_entry_id
                else None
            ),
            "account_credit_consumption_ledger_entry_id": (
                str(self.allocation.consumption_ledger_entry_id)
                if self.allocation.consumption_ledger_entry_id
                else None
            ),
            "access_consequence": (
                self.preview.access_consequence
                if self.preview
                else "recheck_after_receivable_allocation"
            ),
        }


@dataclass(frozen=True)
class PaymentRefundInvoiceEffect:
    invoice_id: UUID
    invoice_number: str | None
    receivable_before: Decimal
    receivable_after: Decimal
    refund_attributed: Decimal


@dataclass(frozen=True)
class PaymentRefundPreview:
    payment_id: UUID
    account_id: UUID
    currency: str
    payment_gross: Decimal
    refunded_before: Decimal
    refundable_before: Decimal
    refund_amount: Decimal
    refunded_after: Decimal
    payment_net_after: Decimal
    status_after: PaymentStatus
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    account_credit_consumption: Decimal
    invoice_effects: tuple[PaymentRefundInvoiceEffect, ...]
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class PaymentRefundResult:
    refund: PaymentRefund
    payment: Payment
    ledger_entry: LedgerEntry
    credit_consumption_ledger_entry: LedgerEntry | None
    preview: PaymentRefundPreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "refund_id": str(self.refund.id),
            "payment_id": str(self.payment.id),
            "ledger_entry_id": str(self.ledger_entry.id),
            "credit_consumption_ledger_entry_id": (
                str(self.credit_consumption_ledger_entry.id)
                if self.credit_consumption_ledger_entry
                else None
            ),
            "amount": str(self.refund.amount),
            "currency": self.refund.currency,
            "origin": self.refund.origin.value,
            "provider_event_id": (
                str(self.refund.provider_event_id)
                if self.refund.provider_event_id
                else None
            ),
            "preview_fingerprint": self.refund.preview_fingerprint,
            "access_consequence": (
                self.preview.access_consequence
                if self.preview
                else "recheck_after_refund"
            ),
        }


@dataclass(frozen=True)
class PaymentRefundEvidenceInspection:
    payment_id: UUID
    recorded_refund_total: Decimal
    linked_ledger_entry_ids: tuple[UUID, ...]
    unlinked_ledger_entry_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class ReversalCapability:
    allowed: bool
    reason: str | None


@dataclass(frozen=True)
class PaymentReversalPreview:
    payment_id: UUID
    account_id: UUID
    currency: str
    payment_gross: Decimal
    refunded_before: Decimal
    payment_net_before: Decimal
    reversal_amount: Decimal
    status_after: PaymentStatus
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    account_credit_before: Decimal
    account_credit_after: Decimal
    account_credit_consumption: Decimal
    invoice_effects: tuple[PaymentRefundInvoiceEffect, ...]
    ledger_entry_type: LedgerEntryType
    ledger_source: LedgerSource
    ledger_amount: Decimal
    access_consequence: str
    fingerprint: str


@dataclass(frozen=True)
class PaymentReversalResult:
    reversal: PaymentReversal
    payment: Payment
    ledger_entry: LedgerEntry
    credit_consumption_ledger_entry: LedgerEntry | None
    preview: PaymentReversalPreview | None
    idempotent_replay: bool = False

    def audit_metadata(self) -> dict[str, object]:
        return {
            "reversal_id": str(self.reversal.id),
            "payment_id": str(self.payment.id),
            "ledger_entry_id": str(self.ledger_entry.id),
            "credit_consumption_ledger_entry_id": (
                str(self.credit_consumption_ledger_entry.id)
                if self.credit_consumption_ledger_entry
                else None
            ),
            "amount": str(self.reversal.amount),
            "currency": self.reversal.currency,
            "origin": self.reversal.origin.value,
            "provider_event_id": (
                str(self.reversal.provider_event_id)
                if self.reversal.provider_event_id
                else None
            ),
            "preview_fingerprint": self.reversal.preview_fingerprint,
            "access_consequence": (
                self.preview.access_consequence
                if self.preview
                else "recheck_after_payment_reversal"
            ),
        }


@dataclass(frozen=True)
class PaymentReversalEvidenceInspection:
    payment_id: UUID
    payment_status: PaymentStatus
    payment_net_amount: Decimal
    linked_ledger_entry_ids: tuple[UUID, ...]
    unlinked_candidate_ledger_entry_ids: tuple[UUID, ...]


# Allowed payment status transitions for the gateway/webhook-driven
# ``mark_status`` path. Gateways re-deliver and deliver out of order, so a late
# ``charge.success`` after a refund, or a late ``charge.failed`` after success,
# must NOT regress committed financial state. Refund, reversal, and cancellation
# outcomes are sinks; ``succeeded`` cannot go to ``failed`` here.
_ALLOWED_PAYMENT_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    PaymentStatus.pending: {
        PaymentStatus.succeeded,
        PaymentStatus.failed,
        PaymentStatus.canceled,
    },
    PaymentStatus.failed: {PaymentStatus.succeeded, PaymentStatus.canceled},
    PaymentStatus.succeeded: {
        PaymentStatus.refunded,
        PaymentStatus.partially_refunded,
    },
    # succeeded -> failed is deliberately ABSENT. This table guards ``mark_status``,
    # which is what provider webhooks drive, and a replayed or out-of-order webhook
    # must never regress a succeeded payment to failed
    # (see tests/test_payment_mark_status_guard.py).
    #
    # A genuine chargeback or bank reversal is a deliberate domain operation, not a
    # status flip, and goes through ``PaymentReversals`` — which requires preview,
    # confirmation, exact evidence, and idempotency. The escape hatch is the domain
    # operation, not a hole in the transition table.
    PaymentStatus.partially_refunded: {PaymentStatus.refunded},
    PaymentStatus.refunded: set(),
    PaymentStatus.reversed: set(),
    PaymentStatus.canceled: set(),
}


class PaymentMethods(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentMethodCreate):
        # Exactly one owner: account (customer subscriber) or reseller org
        # (Layer 3 #329). Validate the account only when account-owned.
        if (payload.account_id is None) == (payload.reseller_id is None):
            raise HTTPException(
                status_code=400,
                detail="Exactly one of account_id or reseller_id is required",
            )
        if payload.account_id is not None:
            _validate_account(db, str(payload.account_id))
        if payload.payment_channel_id:
            _validate_payment_channel(db, str(payload.payment_channel_id))
        if payload.is_default:
            owner_filter = (
                PaymentMethod.account_id == payload.account_id
                if payload.account_id is not None
                else PaymentMethod.reseller_id == payload.reseller_id
            )
            db.query(PaymentMethod).filter(
                owner_filter,
                PaymentMethod.is_default.is_(True),
            ).update({"is_default": False})
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "method_type" not in fields_set:
            default_method = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_payment_method_type"
            )
            if default_method:
                data["method_type"] = validate_enum(
                    default_method, PaymentMethodType, "method_type"
                )
        if data.get("token"):
            data["token"] = encrypt_credential(data["token"])
        method = PaymentMethod(**data)
        db.add(method)
        db.commit()
        db.refresh(method)
        return method

    @staticmethod
    def get(db: Session, method_id: str):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        return method

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentMethod)
        if account_id:
            query = query.filter(PaymentMethod.account_id == account_id)
        if is_active is None:
            query = query.filter(PaymentMethod.is_active.is_(True))
        else:
            query = query.filter(PaymentMethod.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentMethod.created_at,
                "method_type": PaymentMethod.method_type,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, method_id: str, payload: PaymentMethodUpdate):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = data.get("account_id", method.account_id)
        # Reseller-org-owned methods (Layer 3 #329) have no account_id; only
        # validate/scope by account when account-owned, else by reseller.
        if account_id is not None:
            _validate_account(db, str(account_id))
        if "payment_channel_id" in data:
            _validate_payment_channel(
                db,
                str(data["payment_channel_id"]) if data["payment_channel_id"] else None,
            )
        if data.get("is_default"):
            owner_filter = (
                PaymentMethod.account_id == account_id
                if account_id is not None
                else PaymentMethod.reseller_id == method.reseller_id
            )
            db.query(PaymentMethod).filter(
                owner_filter,
                PaymentMethod.id != method.id,
                PaymentMethod.is_default.is_(True),
            ).update({"is_default": False})
        if "token" in data and data["token"]:
            data["token"] = encrypt_credential(data["token"])
        for key, value in data.items():
            setattr(method, key, value)
        db.commit()
        db.refresh(method)
        return method

    @staticmethod
    def get_decrypted_token(db: Session, method_id: str) -> str | None:
        """Retrieve and decrypt the payment token for a payment method."""
        method = get_by_id(db, PaymentMethod, method_id)
        if not method or not method.token:
            return None
        return decrypt_credential(method.token)

    @staticmethod
    def delete(db: Session, method_id: str):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        method.is_active = False
        db.commit()


class BankAccounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: BankAccountCreate):
        _validate_account(db, str(payload.account_id))
        data = payload.model_dump()
        fields_set = payload.model_fields_set
        if "account_type" not in fields_set:
            default_account_type = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_bank_account_type"
            )
            if default_account_type:
                data["account_type"] = validate_enum(
                    default_account_type, BankAccountType, "account_type"
                )
        if payload.payment_method_id:
            method = get_by_id(db, PaymentMethod, payload.payment_method_id)
            if not method:
                raise HTTPException(status_code=404, detail="Payment method not found")
            if method.account_id != payload.account_id:
                raise HTTPException(
                    status_code=400, detail="Payment method does not belong to account"
                )
        if payload.is_default:
            db.query(BankAccount).filter(
                BankAccount.account_id == payload.account_id,
                BankAccount.is_default.is_(True),
            ).update({"is_default": False})
        if data.get("token"):
            data["token"] = encrypt_credential(data["token"])
        bank_account = BankAccount(**data)
        db.add(bank_account)
        db.commit()
        db.refresh(bank_account)
        return bank_account

    @staticmethod
    def get(db: Session, bank_account_id: str):
        bank_account = get_by_id(db, BankAccount, bank_account_id)
        if not bank_account:
            raise HTTPException(status_code=404, detail="Bank account not found")
        return bank_account

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(BankAccount)
        if account_id:
            query = query.filter(BankAccount.account_id == account_id)
        if is_active is None:
            query = query.filter(BankAccount.is_active.is_(True))
        else:
            query = query.filter(BankAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": BankAccount.created_at, "bank_name": BankAccount.bank_name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, bank_account_id: str, payload: BankAccountUpdate):
        bank_account = get_by_id(db, BankAccount, bank_account_id)
        if not bank_account:
            raise HTTPException(status_code=404, detail="Bank account not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = data.get("account_id", bank_account.account_id)
        _validate_account(db, str(account_id))
        payment_method_id = data.get(
            "payment_method_id", bank_account.payment_method_id
        )
        if payment_method_id:
            method = get_by_id(db, PaymentMethod, payment_method_id)
            if not method:
                raise HTTPException(status_code=404, detail="Payment method not found")
            if method.account_id != account_id:
                raise HTTPException(
                    status_code=400, detail="Payment method does not belong to account"
                )
        if data.get("is_default"):
            db.query(BankAccount).filter(
                BankAccount.account_id == account_id,
                BankAccount.id != bank_account.id,
                BankAccount.is_default.is_(True),
            ).update({"is_default": False})
        if "token" in data and data["token"]:
            data["token"] = encrypt_credential(data["token"])
        for key, value in data.items():
            setattr(bank_account, key, value)
        db.commit()
        db.refresh(bank_account)
        return bank_account

    @staticmethod
    def delete(db: Session, bank_account_id: str):
        bank_account = get_by_id(db, BankAccount, bank_account_id)
        if not bank_account:
            raise HTTPException(status_code=404, detail="Bank account not found")
        bank_account.is_active = False
        db.commit()


def _create_payment_ledger_entry(
    db: Session,
    payment: Payment,
    invoice: Invoice | None = None,
    allocation_amount: Decimal | None = None,
) -> LedgerEntry | None:
    """Create a ledger entry for a payment or allocation.

    The ledger entry's ``account_id`` follows the invoice's subscriber when
    allocating to a specific invoice (correct for consolidated payments, where
    the payment itself has no single account). Unallocated-credit entries are
    only written for account-scoped payments; consolidated-payment surplus is
    held on ``BillingAccount.balance`` instead.
    """
    if invoice is None and payment.account_id is None:
        # Consolidated payment remainder goes to BillingAccount.balance,
        # not to a per-subscriber ledger entry.
        return None

    # Idempotency check: skip if an active ledger entry already exists for this
    # payment/invoice. If a prior allocation was voided/refunded, the soft
    # deleted entry can be reactivated by the caller below.
    existing_entry = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id == (invoice.id if invoice else None))
        .filter(LedgerEntry.source == LedgerSource.payment)
        .filter(LedgerEntry.is_active.is_(True))
        .first()
    )
    if existing_entry:
        return existing_entry

    amount = allocation_amount if allocation_amount is not None else payment.amount
    memo = f"Payment {payment.id}"
    if invoice:
        memo = f"Payment {payment.id} applied to Invoice {invoice.invoice_number or invoice.id}"

    account_id = invoice.account_id if invoice is not None else payment.account_id

    entry = LedgerEntry(
        account_id=account_id,
        invoice_id=invoice.id if invoice else None,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=round_money(to_decimal(amount)),
        currency=payment.currency or "NGN",
        memo=memo,
    )
    db.add(entry)
    return entry


def _find_payment_allocation(
    db: Session,
    payment: Payment,
    invoice: Invoice,
) -> PaymentAllocation | None:
    """Return the active allocation for a payment/invoice pair, if present."""
    return (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .first()
    )


def _find_inactive_payment_allocation(
    db: Session,
    payment: Payment,
    invoice: Invoice,
) -> PaymentAllocation | None:
    """Return a soft-deleted allocation for re-use after void/refund reversal."""
    return (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(False))
        .first()
    )


def _apply_payment_allocation(
    db: Session,
    payment: Payment,
    invoice: Invoice,
    amount: Decimal,
    *,
    memo: str | None = None,
) -> tuple[PaymentAllocation, Decimal]:
    """Create or reuse one payment allocation and its invoice ledger entry.

    Returns the allocation plus the amount that should reduce the payment's
    remaining allocatable balance.
    """
    existing = _find_payment_allocation(db, payment, invoice)
    if existing:
        if existing.ledger_entry_id is not None:
            # Idempotent re-runs must not recreate the invoice ledger credit or
            # report the old allocation as newly applied.
            return existing, Decimal("0.00")
        # Pending payments may carry allocation intent, but intent has no money
        # effect. Settlement posts and links the exact ledger row here.
        applied_amount = round_money(to_decimal(amount))
        existing.amount = applied_amount
        existing.memo = memo
        entry = _create_payment_ledger_entry(db, payment, invoice, applied_amount)
        if entry is None:
            raise HTTPException(
                status_code=409,
                detail="Payment allocation ledger evidence could not be created",
            )
        db.flush()
        existing.ledger_entry_id = entry.id
        return existing, applied_amount

    applied_amount = round_money(to_decimal(amount))
    inactive = _find_inactive_payment_allocation(db, payment, invoice)
    if inactive:
        inactive.amount = applied_amount
        inactive.memo = memo
        inactive.is_active = True
        entry = _create_payment_ledger_entry(db, payment, invoice, applied_amount)
        if entry is not None:
            entry.amount = applied_amount
            entry.currency = payment.currency or invoice.currency or "NGN"
            entry.is_active = True
            db.flush()
            inactive.ledger_entry_id = entry.id
        return inactive, applied_amount

    allocation = PaymentAllocation(
        payment_id=payment.id,
        invoice_id=invoice.id,
        amount=applied_amount,
        memo=memo,
    )
    db.add(allocation)
    entry = _create_payment_ledger_entry(db, payment, invoice, applied_amount)
    if entry is None:
        raise HTTPException(
            status_code=409,
            detail="Payment allocation ledger evidence could not be created",
        )
    db.flush()
    allocation.ledger_entry_id = entry.id
    return allocation, applied_amount


def _record_unallocated_payment_credit(
    db: Session,
    payment: Payment,
    remaining: Decimal,
) -> LedgerEntry | None:
    """Record the unallocated payment surplus.

    For an account-scoped payment, this writes a ledger entry against the
    payer's subscriber account. For a consolidated (billing-account-scoped)
    payment, the surplus increments ``BillingAccount.balance`` instead.
    """
    remaining = round_money(to_decimal(remaining))
    if remaining <= 0:
        return None
    if payment.billing_account_id is not None:
        from app.services.billing.billing_accounts import BillingAccounts

        BillingAccounts.credit_balance(db, str(payment.billing_account_id), remaining)
        return None
    entry = _create_payment_ledger_entry(db, payment, None, remaining)
    if entry is None:
        raise HTTPException(
            status_code=409,
            detail="Unallocated payment ledger evidence could not be created",
        )
    return entry


def _open_invoice_balance_exists(db: Session, account_id, currency: str) -> bool:
    return (
        db.query(Invoice.id)
        .filter(Invoice.account_id == account_id)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            )
        )
        .filter(Invoice.currency == currency)
        .filter(Invoice.balance_due > Decimal("0.00"))
        .first()
        is not None
    )


def _existing_prepaid_renewal_debit(
    db: Session, payment: Payment
) -> LedgerEntry | None:
    return (
        db.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id.is_(None))
        .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
        .filter(LedgerEntry.source == LedgerSource.invoice)
        .filter(LedgerEntry.is_active.is_(True))
        .first()
    )


def _active_prepaid_monthly_subscription(
    db: Session,
    account_id,
) -> Subscription | None:
    rows = (
        db.query(Subscription)
        .join(CatalogOffer, CatalogOffer.id == Subscription.offer_id)
        .filter(Subscription.subscriber_id == account_id)
        .filter(Subscription.status == SubscriptionStatus.active)
        .filter(Subscription.billing_mode == BillingMode.prepaid)
        .filter(CatalogOffer.billing_cycle == BillingCycle.monthly)
        .filter(CatalogOffer.is_active.is_(True))
        .order_by(Subscription.created_at.asc(), Subscription.id.asc())
        .limit(2)
        .all()
    )
    if len(rows) != 1:
        return None
    return rows[0]


def _prepaid_monthly_charge_amount(
    db: Session,
    subscription: Subscription,
    effective_at: datetime,
) -> tuple[Decimal, str, BillingCycle] | None:
    from app.services.billing._common import _calculate_tax_amount
    from app.services.billing_automation import (
        _default_tax_application,
        _effective_unit_price,
        _resolve_price,
        _resolve_tax_rate_id,
    )

    amount, currency, cycle = _resolve_price(db, subscription)
    if amount is None:
        return None
    effective_cycle = cycle or BillingCycle.monthly
    if effective_cycle != BillingCycle.monthly:
        return None
    base = _effective_unit_price(subscription, amount, effective_at)
    tax_rate_id = _resolve_tax_rate_id(db, subscription)
    if not tax_rate_id:
        return base, currency or "NGN", effective_cycle
    tax_rate = db.get(TaxRate, tax_rate_id)
    if tax_rate is None:
        return base, currency or "NGN", effective_cycle
    tax_application = _default_tax_application(db)
    tax_amount = _calculate_tax_amount(
        base, Decimal(str(tax_rate.rate)), tax_application
    )
    total = (
        base
        if tax_application == TaxApplication.inclusive
        else round_money(base + tax_amount)
    )
    return total, currency or "NGN", effective_cycle


def apply_prepaid_service_credit(
    db: Session,
    payment: Payment,
) -> bool:
    """Consume unallocated credit for one active prepaid monthly renewal.

    This is intentionally narrow: it runs only for succeeded account-scoped
    payments, only when no open invoice remains, and only when exactly one active
    prepaid monthly service exists. It leaves ambiguous wallet credit untouched.
    """
    if payment.account_id is None or payment.status != PaymentStatus.succeeded:
        return False
    if _existing_prepaid_renewal_debit(db, payment):
        return False
    currency = payment.currency or "NGN"
    if _open_invoice_balance_exists(db, payment.account_id, currency):
        return False
    subscription = _active_prepaid_monthly_subscription(db, payment.account_id)
    if subscription is None:
        return False

    effective_at = payment.paid_at or datetime.now(UTC)
    charge = _prepaid_monthly_charge_amount(db, subscription, effective_at)
    if charge is None:
        return False
    charge_amount, charge_currency, cycle = charge
    if charge_currency != currency:
        return False

    from app.services.billing._common import get_account_credit_balance
    from app.services.billing_automation import (
        _as_utc,
        _paid_coverage_end_for_subscription,
        _period_end,
    )

    # effective_at is never None here (payment.paid_at or now()), so _as_utc is
    # non-None — assert to narrow for the type checker.
    effective_utc = _as_utc(effective_at)
    assert effective_utc is not None
    paid_at_day = effective_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    next_billing = _as_utc(subscription.next_billing_at) or paid_at_day
    period_start = max(next_billing, paid_at_day)
    period_end = _period_end(period_start, cycle)
    paid_through = _paid_coverage_end_for_subscription(
        db,
        subscription.id,
        subscription.subscriber_id,
        period_start,
        period_end,
    )
    if paid_through and paid_through > period_start:
        if subscription.next_billing_at is None or next_billing < paid_through:
            subscription.next_billing_at = paid_through
        return False

    db.flush()
    available = get_account_credit_balance(
        db, str(payment.account_id), currency=currency
    )
    if round_money(available) < charge_amount:
        return False

    ledger_entry = LedgerEntry(
        account_id=payment.account_id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.invoice,
        category=LedgerCategory.internet_service,
        amount=charge_amount,
        currency=currency,
        effective_date=effective_at,
        memo=f"Prepaid service renewal {period_start.date()} - {period_end.date()}",
    )
    db.add(ledger_entry)
    db.flush()
    ensure_prepaid_entitlement_for_wallet_debit(
        db,
        subscription=subscription,
        ledger_entry=ledger_entry,
        starts_at=period_start,
        ends_at=period_end,
    )
    subscription.next_billing_at = period_end

    from app.services.account_lifecycle import compute_account_status

    compute_account_status(db, str(payment.account_id))
    return True


def _apply_previewed_prepaid_service_effect(
    db: Session,
    payment: Payment,
    preview: PaymentCreationPreview,
) -> LedgerEntry | None:
    apply_prepaid_service_credit(db, payment)
    entry = _existing_prepaid_renewal_debit(db, payment)
    actual = (
        round_money(to_decimal(entry.amount)) if entry is not None else Decimal("0.00")
    )
    expected = (
        preview.prepaid_service_effect.charge_amount
        if preview.prepaid_service_effect is not None
        else Decimal("0.00")
    )
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail="Prepaid service consequence no longer matches preview",
        )
    return entry


def _latest_successful_invoice_payment(db: Session, invoice: Invoice) -> Payment | None:
    return (
        db.query(Payment)
        .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .filter(PaymentAllocation.is_active.is_(True))
        .filter(Payment.status == PaymentStatus.succeeded)
        .filter(Payment.is_active.is_(True))
        .order_by(
            func.coalesce(
                Payment.paid_at,
                PaymentAllocation.created_at,
                Payment.created_at,
            ).desc(),
            Payment.created_at.desc(),
            Payment.id.desc(),
        )
        .first()
    )


def _invoice_subscription_lines(
    db: Session, invoice: Invoice
) -> tuple[Subscription, list[InvoiceLine]] | None:
    lines = (
        db.query(InvoiceLine)
        .filter(InvoiceLine.invoice_id == invoice.id)
        .filter(InvoiceLine.subscription_id.is_not(None))
        .filter(InvoiceLine.is_active.is_(True))
        .all()
    )
    subscription_ids = {line.subscription_id for line in lines}
    if len(subscription_ids) != 1:
        return None
    subscription = db.get(Subscription, next(iter(subscription_ids)))
    if subscription is None or subscription.billing_mode != BillingMode.prepaid:
        return None
    return subscription, lines


def _base_subscription_invoice_lines(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    base_lines = [
        line
        for line in lines
        if (line.metadata_ or {}).get("kind") == "base_subscription"
    ]
    if base_lines:
        return base_lines
    billable_lines = [line for line in lines if round_money(line.amount) > 0]
    if len(billable_lines) == 1:
        return billable_lines
    if len(lines) == 1:
        return lines
    return []


def _prepaid_invoice_needs_payment_date_anchor(
    subscription: Subscription,
    invoice: Invoice,
    paid_at_day: datetime,
) -> bool:
    period_start = invoice.billing_period_start
    if period_start is None:
        return False
    period_start = (
        period_start if period_start.tzinfo else period_start.replace(tzinfo=UTC)
    )
    if paid_at_day <= period_start:
        return False

    lapsed_statuses = {
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.expired,
    }
    if subscription.status in lapsed_statuses:
        return True

    next_billing = subscription.next_billing_at
    if next_billing is not None:
        next_billing = (
            next_billing if next_billing.tzinfo else next_billing.replace(tzinfo=UTC)
        )
        if next_billing <= paid_at_day:
            return True

    period_end = invoice.billing_period_end
    if period_end is not None:
        period_end = period_end if period_end.tzinfo else period_end.replace(tzinfo=UTC)
        if period_end <= paid_at_day:
            return True

    return False


def _prepaid_extension_delta_after_invoice(
    invoice: Invoice, subscription: Subscription
) -> timedelta:
    period_end = invoice.billing_period_end
    next_billing = subscription.next_billing_at
    if period_end is None or next_billing is None:
        return timedelta(0)
    period_end = period_end if period_end.tzinfo else period_end.replace(tzinfo=UTC)
    next_billing = (
        next_billing if next_billing.tzinfo else next_billing.replace(tzinfo=UTC)
    )
    if next_billing <= period_end:
        return timedelta(0)
    return next_billing - period_end


def _reanchor_paid_prepaid_invoice_if_lapsed(db: Session, invoice: Invoice) -> bool:
    """Start lapsed prepaid renewals from the settlement date.

    Prepaid customers should not lose paid entitlement to a historical unpaid
    period after they have already been suspended or otherwise lapsed. When a
    payment fully settles that renewal invoice, move the covered period to the
    payment date and advance the subscription from there.
    """
    if invoice.status != InvoiceStatus.paid:
        return False
    if invoice.billing_period_start is None or invoice.billing_period_end is None:
        return False

    payment = _latest_successful_invoice_payment(db, invoice)
    if payment is None:
        return False
    effective_at = payment.paid_at or payment.created_at or datetime.now(UTC)
    paid_at_utc = (
        effective_at if effective_at.tzinfo else effective_at.replace(tzinfo=UTC)
    )
    paid_at_day = paid_at_utc.replace(hour=0, minute=0, second=0, microsecond=0)

    resolved = _invoice_subscription_lines(db, invoice)
    if resolved is None:
        return False
    subscription, lines = resolved
    if not _prepaid_invoice_needs_payment_date_anchor(
        subscription, invoice, paid_at_day
    ):
        return False

    base_lines = _base_subscription_invoice_lines(lines)
    if not base_lines:
        return False

    from app.services.billing_automation import _period_end

    cycle = (
        subscription.offer.billing_cycle
        if subscription.offer and subscription.offer.billing_cycle
        else BillingCycle.monthly
    )
    extension_delta = _prepaid_extension_delta_after_invoice(invoice, subscription)
    old_period_start = invoice.billing_period_start
    old_period_end = invoice.billing_period_end
    new_period_start = paid_at_day
    new_period_end = _period_end(new_period_start, cycle)
    if old_period_start == new_period_start and old_period_end == new_period_end:
        return False

    invoice.billing_period_start = new_period_start
    invoice.billing_period_end = new_period_end
    offer_name = subscription.offer.name if subscription.offer else "Subscription"
    for line in base_lines:
        metadata = dict(line.metadata_ or {})
        metadata["kind"] = metadata.get("kind") or "base_subscription"
        metadata["billing_period_start"] = new_period_start.isoformat()
        metadata["billing_period_end"] = new_period_end.isoformat()
        line.metadata_ = metadata
        line.description = (
            f"{offer_name} ({new_period_start.date()} - {new_period_end.date()})"
        )

    target_next_billing = new_period_end + extension_delta
    current_next = subscription.next_billing_at
    if current_next is None:
        subscription.next_billing_at = target_next_billing
    else:
        current_next = (
            current_next if current_next.tzinfo else current_next.replace(tzinfo=UTC)
        )
        if current_next < target_next_billing:
            subscription.next_billing_at = target_next_billing

    logger.info(
        "prepaid_invoice_reanchored_to_payment_date",
        extra={
            "event": "prepaid_invoice_reanchored_to_payment_date",
            "invoice_id": str(invoice.id),
            "subscription_id": str(subscription.id),
            "payment_id": str(payment.id),
            "old_period_start": old_period_start.isoformat(),
            "old_period_end": old_period_end.isoformat(),
            "new_period_start": new_period_start.isoformat(),
            "new_period_end": new_period_end.isoformat(),
            "extension_delta_seconds": extension_delta.total_seconds(),
            "new_next_billing_at": target_next_billing.isoformat(),
        },
    )
    return True


def _finalize_invoice_payment_effects(db: Session, invoice: Invoice) -> None:
    """Recompute invoice totals, restore eligible service, then derive account status."""
    _recalculate_invoice_totals(db, invoice)
    # Sessions use autoflush=False, so make the recomputed balance visible
    # before has_overdue_balance queries the database.
    db.flush()

    if invoice.status == InvoiceStatus.paid:
        _reanchor_paid_prepaid_invoice_if_lapsed(db, invoice)
        ensure_prepaid_entitlements_for_paid_invoice(db, invoice)

        from app.services import collections as collections_service

        if not collections_service.has_overdue_balance(db, str(invoice.account_id)):
            collections_service.restore_account_services(
                db, str(invoice.account_id), invoice_id=str(invoice.id)
            )
    else:
        # The invoice stopped being paid — a refund, a chargeback, or an
        # allocation moved away. The service it funded has to stop being funded
        # too. Without this the entitlement stayed active forever and prepaid
        # funding kept counting it as paid coverage, so a refunded customer kept
        # the service free for the whole period.
        revoke_prepaid_entitlements_for_unpaid_invoice(db, invoice)

    from app.services.account_lifecycle import compute_account_status

    compute_account_status(db, str(invoice.account_id))


def _primary_allocation_invoice_id(payment: Payment) -> str | None:
    if not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


def _emit_consolidated_payment_events(
    db: Session, payment: Payment, allocations: list[PaymentAllocation]
) -> None:
    """Emit per-subscriber payment.received events plus one aggregate event.

    Per-subscriber events keep existing handlers (notifications, dunning, etc.)
    working without changes. The aggregate event is for handlers that need the
    consolidated view.
    """
    breakdown: list[dict[str, str]] = []
    for allocation in allocations:
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        if invoice is None:
            continue
        breakdown.append(
            {
                "account_id": str(invoice.account_id),
                "invoice_id": str(invoice.id),
                "amount": str(allocation.amount),
            }
        )
        emit_event(
            db,
            EventType.payment_received,
            {
                "payment_id": str(payment.id),
                "amount": str(allocation.amount),
                "currency": payment.currency,
                "invoice_id": str(invoice.id),
                "status": payment.status.value if payment.status else None,
                "billing_account_id": str(payment.billing_account_id),
            },
            account_id=invoice.account_id,
            invoice_id=invoice.id,
        )

    emit_event(
        db,
        EventType.billing_account_payment_received,
        {
            "payment_id": str(payment.id),
            "billing_account_id": str(payment.billing_account_id),
            "total": str(payment.amount) if payment.amount else None,
            "currency": payment.currency,
            "status": payment.status.value if payment.status else None,
            "allocations": breakdown,
        },
    )


def _creation_request_payload(
    payload: PaymentCreationPreviewRequest | PaymentCreationConfirm,
) -> PaymentCreate:
    return PaymentCreate(
        **payload.model_dump(
            exclude={"auto_allocate", "preview_fingerprint", "idempotency_key"}
        )
    )


def _preview_prepaid_service_effect(
    db: Session,
    *,
    payload: PaymentCreate,
    effects: tuple[PaymentCreationAllocationEffect, ...],
    account_credit_available: Decimal,
) -> PaymentPrepaidServiceEffect | None:
    if payload.account_id is None or payload.status != PaymentStatus.succeeded:
        return None
    currency = payload.currency.upper()
    effect_balances = {effect.invoice_id: effect.receivable_after for effect in effects}
    open_invoices = (
        db.query(Invoice)
        .filter(Invoice.account_id == payload.account_id)
        .filter(Invoice.is_active.is_(True))
        .filter(
            Invoice.status.in_(
                [
                    InvoiceStatus.issued,
                    InvoiceStatus.partially_paid,
                    InvoiceStatus.overdue,
                ]
            )
        )
        .filter(Invoice.currency == currency)
        .filter(Invoice.balance_due > Decimal("0.00"))
        .all()
    )
    if any(
        effect_balances.get(invoice.id, round_money(to_decimal(invoice.balance_due)))
        > Decimal("0.00")
        for invoice in open_invoices
    ):
        return None
    subscription = _active_prepaid_monthly_subscription(db, payload.account_id)
    if subscription is None:
        return None
    from app.services.billing_automation import (
        _as_utc,
        _paid_coverage_end_for_subscription,
        _period_end,
    )

    effective_at = payload.paid_at or datetime.now(UTC)
    effective_utc = _as_utc(effective_at)
    assert effective_utc is not None
    charge = _prepaid_monthly_charge_amount(db, subscription, effective_utc)
    if charge is None:
        return None
    charge_amount, charge_currency, cycle = charge
    if charge_currency != currency:
        return None
    paid_at_day = effective_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    next_billing = _as_utc(subscription.next_billing_at) or paid_at_day
    period_start = max(next_billing, paid_at_day)
    period_end = _period_end(period_start, cycle)
    paid_through = _paid_coverage_end_for_subscription(
        db,
        subscription.id,
        subscription.subscriber_id,
        period_start,
        period_end,
    )
    if paid_through and paid_through > period_start:
        return PaymentPrepaidServiceEffect(
            subscription_id=subscription.id,
            charge_amount=Decimal("0.00"),
            period_start=period_start,
            period_end=paid_through,
            ledger_entry_type=None,
            ledger_source=None,
            consequence="advance_to_existing_paid_coverage",
        )
    if round_money(account_credit_available) < round_money(charge_amount):
        return None
    return PaymentPrepaidServiceEffect(
        subscription_id=subscription.id,
        charge_amount=round_money(charge_amount),
        period_start=period_start,
        period_end=period_end,
        ledger_entry_type=LedgerEntryType.debit,
        ledger_source=LedgerSource.invoice,
        consequence="fund_prepaid_service_period",
    )


def _payment_creation_fingerprint(
    *,
    payload: PaymentCreate,
    auto_allocate: bool,
    funding_before: Decimal,
    account_credit_before: Decimal,
    effects: tuple[PaymentCreationAllocationEffect, ...],
    unallocated_amount: Decimal,
    prepaid_service_effect: PaymentPrepaidServiceEffect | None,
) -> str:
    encoded = json.dumps(
        {
            "kind": "payment_creation",
            "account_id": str(payload.account_id),
            "amount": f"{round_money(to_decimal(payload.amount)):.2f}",
            "currency": payload.currency,
            "status": payload.status.value,
            "payment_method_id": (
                str(payload.payment_method_id) if payload.payment_method_id else None
            ),
            "payment_channel_id": (
                str(payload.payment_channel_id) if payload.payment_channel_id else None
            ),
            "collection_account_id": (
                str(payload.collection_account_id)
                if payload.collection_account_id
                else None
            ),
            "provider_id": str(payload.provider_id) if payload.provider_id else None,
            "external_id": payload.external_id,
            "memo": payload.memo,
            "paid_at": payload.paid_at.isoformat() if payload.paid_at else None,
            "auto_allocate": auto_allocate,
            "prepaid_funding_before": f"{funding_before:.2f}",
            "account_credit_before": f"{account_credit_before:.2f}",
            "unallocated_amount": f"{unallocated_amount:.2f}",
            "prepaid_service_effect": (
                {
                    "subscription_id": str(prepaid_service_effect.subscription_id),
                    "charge_amount": f"{prepaid_service_effect.charge_amount:.2f}",
                    "period_start": prepaid_service_effect.period_start.isoformat(),
                    "period_end": prepaid_service_effect.period_end.isoformat(),
                    "consequence": prepaid_service_effect.consequence,
                }
                if prepaid_service_effect
                else None
            ),
            "allocations": [
                {
                    "invoice_id": str(effect.invoice_id),
                    "receivable_before": f"{effect.receivable_before:.2f}",
                    "receivable_after": f"{effect.receivable_after:.2f}",
                    "allocation_amount": f"{effect.allocation_amount:.2f}",
                }
                for effect in effects
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_payment_creation_preview(
    db: Session,
    payload: PaymentCreate,
    *,
    auto_allocate: bool,
) -> PaymentCreationPreview:
    if payload.account_id is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Consolidated billing-account payments require their dedicated "
                "owner workflow"
            ),
        )
    if payload.status not in {PaymentStatus.pending, PaymentStatus.succeeded}:
        raise HTTPException(
            status_code=409,
            detail=(
                "Manual payment creation may record pending intent or confirmed "
                "settlement only"
            ),
        )
    _validate_payment_linkages(
        db,
        str(payload.account_id),
        None,
        str(payload.payment_method_id) if payload.payment_method_id else None,
    )
    _validate_payment_provider(
        db, str(payload.provider_id) if payload.provider_id else None
    )
    amount = round_money(to_decimal(payload.amount))
    currency = payload.currency.upper()
    funding_before = calculate_customer_balance(
        db, payload.account_id, currency=currency
    )
    account_credit_before = get_account_credit_balance(
        db, str(payload.account_id), currency=currency
    )
    if payload.status == PaymentStatus.pending:
        if payload.allocations:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Pending payment creation is intent only and cannot carry "
                    "invoice allocations; record allocation intent after creating "
                    "the payment or confirm allocations during settlement"
                ),
            )
        fingerprint = _payment_creation_fingerprint(
            payload=payload,
            auto_allocate=auto_allocate,
            funding_before=funding_before,
            account_credit_before=account_credit_before,
            effects=(),
            unallocated_amount=Decimal("0.00"),
            prepaid_service_effect=None,
        )
        return PaymentCreationPreview(
            account_id=payload.account_id,
            amount=amount,
            currency=currency,
            status=payload.status,
            prepaid_funding_before=funding_before,
            prepaid_funding_after=funding_before,
            account_credit_before=account_credit_before,
            account_credit_after=account_credit_before,
            allocation_effects=(),
            unallocated_amount=Decimal("0.00"),
            unallocated_ledger_entry_type=None,
            unallocated_ledger_source=None,
            prepaid_service_effect=None,
            access_consequence="none_until_payment_settlement",
            fingerprint=fingerprint,
        )

    remaining = amount
    effects: list[PaymentCreationAllocationEffect] = []
    invoice_requests: list[tuple[Invoice, Decimal]] = []
    if payload.allocations:
        seen: set[UUID] = set()
        for allocation in payload.allocations:
            if allocation.invoice_id in seen:
                raise HTTPException(
                    status_code=400,
                    detail="Payment preview contains a duplicate invoice allocation",
                )
            seen.add(allocation.invoice_id)
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if invoice.account_id != payload.account_id:
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            _validate_invoice_currency(invoice, currency)
            _assert_invoice_allocatable(invoice)
            invoice_requests.append(
                (invoice, round_money(to_decimal(allocation.amount)))
            )
    elif auto_allocate:
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id == payload.account_id)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .filter(Invoice.balance_due > 0)
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
        invoice_requests = [
            (invoice, round_money(to_decimal(invoice.balance_due)))
            for invoice in invoices
            if invoice.currency == currency
        ]

    for invoice, requested in invoice_requests:
        if remaining <= 0:
            break
        receivable_before = round_money(to_decimal(invoice.balance_due))
        allocation_amount = min(remaining, requested, receivable_before)
        if allocation_amount <= 0:
            continue
        effects.append(
            PaymentCreationAllocationEffect(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                receivable_before=receivable_before,
                receivable_after=round_money(receivable_before - allocation_amount),
                allocation_amount=allocation_amount,
            )
        )
        remaining = round_money(remaining - allocation_amount)

    effect_tuple = tuple(effects)
    prepaid_service_effect = _preview_prepaid_service_effect(
        db,
        payload=payload,
        effects=effect_tuple,
        account_credit_available=round_money(account_credit_before + remaining),
    )
    prepaid_charge = (
        prepaid_service_effect.charge_amount
        if prepaid_service_effect is not None
        else Decimal("0.00")
    )
    fingerprint = _payment_creation_fingerprint(
        payload=payload,
        auto_allocate=auto_allocate,
        funding_before=funding_before,
        account_credit_before=account_credit_before,
        effects=effect_tuple,
        unallocated_amount=remaining,
        prepaid_service_effect=prepaid_service_effect,
    )
    return PaymentCreationPreview(
        account_id=payload.account_id,
        amount=amount,
        currency=currency,
        status=payload.status,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=round_money(funding_before + amount - prepaid_charge),
        account_credit_before=account_credit_before,
        account_credit_after=round_money(
            account_credit_before + remaining - prepaid_charge
        ),
        allocation_effects=effect_tuple,
        unallocated_amount=remaining,
        unallocated_ledger_entry_type=(
            LedgerEntryType.credit if remaining > 0 else None
        ),
        unallocated_ledger_source=(LedgerSource.payment if remaining > 0 else None),
        prepaid_service_effect=prepaid_service_effect,
        access_consequence="recheck_after_payment_settlement",
        fingerprint=fingerprint,
    )


def _normalize_payment_creation_key(value: str) -> str:
    key = value.strip()
    if not _REFUND_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail="Payment creation idempotency key must be 16-120 safe characters",
        )
    return key


def _create_account_payment_from_preview(
    db: Session,
    payload: PaymentCreate,
    preview: PaymentCreationPreview,
    *,
    auto_allocate: bool,
    origin: PaymentSettlementOrigin,
    idempotency_key: str | None,
    commit: bool,
) -> PaymentCreationResult:
    assert payload.account_id is not None
    data = payload.model_dump(exclude={"allocations"})
    data["currency"] = preview.currency
    data["auto_allocate_on_settlement"] = auto_allocate
    data["creation_preview_fingerprint"] = preview.fingerprint
    channel = _resolve_payment_channel(
        db,
        str(payload.payment_channel_id) if payload.payment_channel_id else None,
        str(payload.payment_method_id) if payload.payment_method_id else None,
        str(payload.provider_id) if payload.provider_id else None,
    )
    if channel and not payload.payment_channel_id:
        data["payment_channel_id"] = channel.id
    collection_account = _resolve_collection_account(
        db,
        channel,
        preview.currency,
        str(payload.collection_account_id) if payload.collection_account_id else None,
    )
    if collection_account and not payload.collection_account_id:
        data["collection_account_id"] = collection_account.id
    if payload.collection_account_id and not collection_account:
        _validate_collection_account(
            db, str(payload.collection_account_id), preview.currency
        )
    if preview.status == PaymentStatus.succeeded and not data.get("paid_at"):
        data["paid_at"] = datetime.now(UTC)

    payment = Payment(**data)
    db.add(payment)
    db.flush()
    if preview.status == PaymentStatus.pending:
        # Pending is intent only: no allocations, ledger, receivable mutation,
        # access consequence, or payment.received event exists yet.
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action="create_payment_intent",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata_={
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "status": payment.status.value,
                    "preview_fingerprint": preview.fingerprint,
                    "access_consequence": preview.access_consequence,
                },
            ),
        )
        if commit:
            db.commit()
            db.refresh(payment)
        else:
            db.flush()
        return PaymentCreationResult(
            payment=payment,
            settlement=None,
            preview=preview,
        )

    allocations: list[PaymentAllocation] = []
    for effect in preview.allocation_effects:
        invoice = get_by_id(db, Invoice, effect.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        allocation, applied_amount = _apply_payment_allocation(
            db,
            payment,
            invoice,
            effect.allocation_amount,
        )
        if applied_amount != effect.allocation_amount:
            raise HTTPException(
                status_code=409,
                detail="Payment allocation result no longer matches preview",
            )
        allocations.append(allocation)
    unallocated_entry = _record_unallocated_payment_credit(
        db, payment, preview.unallocated_amount
    )
    db.flush()
    for allocation in allocations:
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        if invoice:
            _finalize_invoice_payment_effects(db, invoice)
    prepaid_entry = _apply_previewed_prepaid_service_effect(db, payment, preview)
    if not allocations:
        from app.services.account_lifecycle import compute_account_status

        compute_account_status(db, str(payment.account_id))
    settlement = PaymentSettlement(
        payment_id=payment.id,
        unallocated_ledger_entry_id=(
            unallocated_entry.id if unallocated_entry is not None else None
        ),
        prepaid_ledger_entry_id=(
            prepaid_entry.id if prepaid_entry is not None else None
        ),
        amount=preview.amount,
        unallocated_amount=preview.unallocated_amount,
        prepaid_amount=(
            round_money(to_decimal(prepaid_entry.amount))
            if prepaid_entry is not None
            else Decimal("0.00")
        ),
        currency=preview.currency,
        origin=origin,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=idempotency_key,
    )
    db.add(settlement)
    db.flush()
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="settle",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata_={
                "settlement_id": str(settlement.id),
                "amount": str(settlement.amount),
                "currency": settlement.currency,
                "origin": settlement.origin.value,
                "preview_fingerprint": preview.fingerprint,
                "allocation_ledger_entry_ids": [
                    str(allocation.ledger_entry_id)
                    for allocation in allocations
                    if allocation.ledger_entry_id is not None
                ],
                "unallocated_ledger_entry_id": (
                    str(unallocated_entry.id) if unallocated_entry else None
                ),
                "prepaid_ledger_entry_id": (
                    str(prepaid_entry.id) if prepaid_entry else None
                ),
                "prepaid_amount": (
                    str(prepaid_entry.amount) if prepaid_entry else "0.00"
                ),
                "prepaid_funding_before": str(preview.prepaid_funding_before),
                "prepaid_funding_after": str(preview.prepaid_funding_after),
                "account_credit_before": str(preview.account_credit_before),
                "account_credit_after": str(preview.account_credit_after),
                "access_consequence": preview.access_consequence,
            },
        ),
    )
    allocation_invoice_id = _primary_allocation_invoice_id(payment)
    emit_event(
        db,
        EventType.payment_received,
        {
            "payment_id": str(payment.id),
            "settlement_id": str(settlement.id),
            "amount": str(payment.amount),
            "currency": payment.currency,
            "invoice_id": allocation_invoice_id,
            "status": payment.status.value,
        },
        account_id=payment.account_id,
        invoice_id=allocation_invoice_id,
    )
    if commit:
        db.commit()
        db.refresh(payment)
        db.refresh(settlement)
    else:
        db.flush()
    return PaymentCreationResult(
        payment=payment,
        settlement=settlement,
        preview=preview,
    )


def _existing_payment_settlement_payload(payment: Payment) -> PaymentCreate:
    planned_allocations = [
        PaymentAllocationApply(
            invoice_id=allocation.invoice_id,
            amount=allocation.amount,
            memo=allocation.memo,
        )
        for allocation in payment.allocations
        if allocation.is_active
    ]
    return PaymentCreate(
        account_id=payment.account_id,
        billing_account_id=payment.billing_account_id,
        payment_method_id=payment.payment_method_id,
        payment_channel_id=payment.payment_channel_id,
        collection_account_id=payment.collection_account_id,
        provider_id=payment.provider_id,
        amount=payment.amount,
        currency=payment.currency,
        status=PaymentStatus.succeeded,
        external_id=payment.external_id,
        memo=payment.memo,
        allocations=planned_allocations or None,
    )


def _settle_existing_account_payment(
    db: Session,
    payment: Payment,
    preview: PaymentCreationPreview,
    *,
    origin: PaymentSettlementOrigin,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> PaymentCreationResult:
    if payment.account_id is None:
        raise HTTPException(
            status_code=409,
            detail="Consolidated payments require their dedicated settlement owner",
        )
    if payment.settlement is not None:
        return PaymentCreationResult(
            payment=payment,
            settlement=payment.settlement,
            preview=None,
            idempotent_replay=True,
        )
    legacy_rows = (
        db.query(LedgerEntry.id)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.source == LedgerSource.payment)
        .filter(LedgerEntry.is_active.is_(True))
        .all()
    )
    if legacy_rows:
        raise HTTPException(
            status_code=409,
            detail=(
                "Payment already has unreviewed legacy money evidence; reconcile "
                "it before settlement"
            ),
        )
    payment.status = PaymentStatus.succeeded
    payment.paid_at = payment.paid_at or datetime.now(UTC)
    payment.creation_preview_fingerprint = preview.fingerprint
    allocations: list[PaymentAllocation] = []
    for effect in preview.allocation_effects:
        invoice = get_by_id(db, Invoice, effect.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        allocation, applied_amount = _apply_payment_allocation(
            db, payment, invoice, effect.allocation_amount
        )
        if applied_amount != effect.allocation_amount:
            raise HTTPException(
                status_code=409,
                detail="Payment allocation result no longer matches preview",
            )
        allocations.append(allocation)
    unallocated_entry = _record_unallocated_payment_credit(
        db, payment, preview.unallocated_amount
    )
    db.flush()
    for allocation in allocations:
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        if invoice:
            _finalize_invoice_payment_effects(db, invoice)
    prepaid_entry = _apply_previewed_prepaid_service_effect(db, payment, preview)
    if not allocations:
        from app.services.account_lifecycle import compute_account_status

        compute_account_status(db, str(payment.account_id))
    settlement = PaymentSettlement(
        payment_id=payment.id,
        unallocated_ledger_entry_id=(
            unallocated_entry.id if unallocated_entry is not None else None
        ),
        prepaid_ledger_entry_id=(
            prepaid_entry.id if prepaid_entry is not None else None
        ),
        amount=preview.amount,
        unallocated_amount=preview.unallocated_amount,
        prepaid_amount=(
            round_money(to_decimal(prepaid_entry.amount))
            if prepaid_entry is not None
            else Decimal("0.00")
        ),
        currency=preview.currency,
        origin=origin,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=idempotency_key,
    )
    db.add(settlement)
    db.flush()
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="settle",
            entity_type="payment",
            entity_id=str(payment.id),
            metadata_={
                "settlement_id": str(settlement.id),
                "amount": str(settlement.amount),
                "currency": settlement.currency,
                "origin": settlement.origin.value,
                "preview_fingerprint": preview.fingerprint,
                "allocation_ledger_entry_ids": [
                    str(allocation.ledger_entry_id)
                    for allocation in allocations
                    if allocation.ledger_entry_id is not None
                ],
                "unallocated_ledger_entry_id": (
                    str(unallocated_entry.id) if unallocated_entry else None
                ),
                "prepaid_ledger_entry_id": (
                    str(prepaid_entry.id) if prepaid_entry else None
                ),
                "prepaid_amount": (
                    str(prepaid_entry.amount) if prepaid_entry else "0.00"
                ),
                "access_consequence": preview.access_consequence,
            },
        ),
    )
    allocation_invoice_id = _primary_allocation_invoice_id(payment)
    emit_event(
        db,
        EventType.payment_received,
        {
            "payment_id": str(payment.id),
            "settlement_id": str(settlement.id),
            "amount": str(payment.amount),
            "currency": payment.currency,
            "invoice_id": allocation_invoice_id,
            "from_status": PaymentStatus.pending.value,
            "to_status": PaymentStatus.succeeded.value,
        },
        account_id=payment.account_id,
        invoice_id=allocation_invoice_id,
    )
    if commit:
        db.commit()
        db.refresh(payment)
        db.refresh(settlement)
    else:
        db.flush()
    return PaymentCreationResult(
        payment=payment,
        settlement=settlement,
        preview=preview,
    )


class Payments(ListResponseMixin):
    @staticmethod
    def preview_creation(
        db: Session,
        payload: PaymentCreationPreviewRequest,
    ) -> PaymentCreationPreview:
        return _build_payment_creation_preview(
            db,
            _creation_request_payload(payload),
            auto_allocate=payload.auto_allocate,
        )

    @staticmethod
    def replay_creation_request(
        db: Session,
        payload: PaymentCreate,
        *,
        auto_allocate: bool,
        idempotency_key: str,
    ) -> PaymentCreationResult | None:
        """Replay a trusted caller that lost its original preview response."""
        key = _normalize_payment_creation_key(idempotency_key)
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _PAYMENT_CREATION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(status_code=409, detail="Payment is being recorded")
        payment = get_by_id(db, Payment, reservation.ref_id)
        if not payment:
            raise HTTPException(
                status_code=409, detail="Payment creation evidence is incomplete"
            )
        comparable = (
            payment.account_id == payload.account_id
            and payment.billing_account_id == payload.billing_account_id
            and round_money(to_decimal(payment.amount))
            == round_money(to_decimal(payload.amount))
            and payment.currency == payload.currency.upper()
            and payment.status == payload.status
            and payment.payment_method_id == payload.payment_method_id
            and (
                payload.collection_account_id is None
                or payment.collection_account_id == payload.collection_account_id
            )
            and payment.provider_id == payload.provider_id
            and payment.external_id == payload.external_id
            and payment.memo == payload.memo
            and payment.auto_allocate_on_settlement == auto_allocate
        )
        if not comparable:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different payment request",
            )
        if payload.allocations:
            expected = {
                (allocation.invoice_id, round_money(to_decimal(allocation.amount)))
                for allocation in payload.allocations
            }
            actual = {
                (allocation.invoice_id, round_money(to_decimal(allocation.amount)))
                for allocation in payment.allocations
                if allocation.is_active
            }
            if expected != actual:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was used with different allocations",
                )
        return PaymentCreationResult(
            payment=payment,
            settlement=payment.settlement,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def _creation_replay(
        db: Session,
        *,
        key: str,
        fingerprint: str,
    ) -> PaymentCreationResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _PAYMENT_CREATION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(status_code=409, detail="Payment is being recorded")
        payment = get_by_id(db, Payment, reservation.ref_id)
        if not payment:
            raise HTTPException(
                status_code=409, detail="Payment creation evidence is incomplete"
            )
        settlement = payment.settlement
        recorded_fingerprint = payment.creation_preview_fingerprint
        if recorded_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different payment preview",
            )
        return PaymentCreationResult(
            payment=payment,
            settlement=settlement,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def confirm_creation(
        db: Session,
        payload: PaymentCreationConfirm,
        *,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.manual,
        commit: bool = True,
    ) -> PaymentCreationResult:
        key = _normalize_payment_creation_key(payload.idempotency_key)
        replay = Payments._creation_replay(
            db, key=key, fingerprint=payload.preview_fingerprint
        )
        if replay:
            return replay
        create_payload = _creation_request_payload(payload)
        if create_payload.account_id is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Consolidated billing-account payments require their dedicated "
                    "owner workflow"
                ),
            )
        lock_account(db, str(create_payload.account_id))
        preview = _build_payment_creation_preview(
            db,
            create_payload,
            auto_allocate=payload.auto_allocate,
        )
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = Payments._creation_replay(
            db, key=key, fingerprint=payload.preview_fingerprint
        )
        if replay:
            return replay
        reservation = IdempotencyKey(
            scope=_PAYMENT_CREATION_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=create_payload.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
            result = _create_account_payment_from_preview(
                db,
                create_payload,
                preview,
                auto_allocate=payload.auto_allocate,
                origin=origin,
                idempotency_key=key,
                commit=False,
            )
            reservation.ref_id = str(result.payment.id)
            db.flush()
            if commit:
                db.commit()
                db.refresh(result.payment)
                if result.settlement:
                    db.refresh(result.settlement)
            return result
        except IntegrityError as exc:
            db.rollback()
            replay = Payments._creation_replay(
                db, key=key, fingerprint=payload.preview_fingerprint
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Payment is already being recorded"
            ) from exc
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def preview_settlement(
        db: Session,
        payment_id: str,
    ) -> PaymentCreationPreview:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payments require their dedicated settlement owner",
            )
        if payment.settlement is not None or payment.status == PaymentStatus.succeeded:
            raise HTTPException(status_code=409, detail="Payment is already settled")
        if payment.status not in {PaymentStatus.pending, PaymentStatus.failed}:
            raise HTTPException(
                status_code=409, detail="Payment is not eligible for settlement"
            )
        return _build_payment_creation_preview(
            db,
            _existing_payment_settlement_payload(payment),
            auto_allocate=payment.auto_allocate_on_settlement,
        )

    @staticmethod
    def settle(
        db: Session,
        payment_id: str,
        *,
        preview_fingerprint: str | None = None,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.system,
        idempotency_key: str | None = None,
        commit: bool = True,
    ) -> PaymentCreationResult:
        initial = get_by_id(db, Payment, payment_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payments require their dedicated settlement owner",
            )
        lock_account(db, str(initial.account_id))
        payment = lock_for_update(db, Payment, initial.id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.settlement is not None:
            if (
                preview_fingerprint is not None
                and payment.settlement.preview_fingerprint != preview_fingerprint
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Payment was settled from a different preview",
                )
            return PaymentCreationResult(
                payment=payment,
                settlement=payment.settlement,
                preview=None,
                idempotent_replay=True,
            )
        if payment.status not in {PaymentStatus.pending, PaymentStatus.failed}:
            raise HTTPException(
                status_code=409, detail="Payment is not eligible for settlement"
            )
        preview = _build_payment_creation_preview(
            db,
            _existing_payment_settlement_payload(payment),
            auto_allocate=payment.auto_allocate_on_settlement,
        )
        if preview_fingerprint is not None and (
            preview.fingerprint != preview_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        return _settle_existing_account_payment(
            db,
            payment,
            preview,
            origin=origin,
            idempotency_key=idempotency_key,
            commit=commit,
        )

    @staticmethod
    def inspect_settlement_evidence(
        db: Session,
        payment_id: str,
    ) -> dict[str, object]:
        """List historical candidates without selecting or changing authority."""
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        candidates = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.payment_id == payment.id)
            .filter(LedgerEntry.is_active.is_(True))
            .filter(
                LedgerEntry.source.in_([LedgerSource.payment, LedgerSource.invoice])
            )
            .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
            .all()
        )
        return {
            "payment_id": payment.id,
            "payment_amount": round_money(to_decimal(payment.amount)),
            "currency": payment.currency,
            "already_reconciled": payment.settlement is not None,
            "active_allocation_ids": [
                allocation.id
                for allocation in payment.allocations
                if allocation.is_active
            ],
            "candidate_entries": [
                {
                    "ledger_entry_id": entry.id,
                    "invoice_id": entry.invoice_id,
                    "entry_type": entry.entry_type,
                    "source": entry.source,
                    "amount": round_money(to_decimal(entry.amount)),
                    "currency": entry.currency,
                }
                for entry in candidates
            ],
        }

    @staticmethod
    def reconcile_settlement_evidence(
        db: Session,
        payment_id: str,
        payload: PaymentSettlementReconciliationRequest,
        *,
        commit: bool = True,
    ) -> PaymentSettlement:
        """Attach explicitly reviewed historical rows; never infer or post money."""
        initial = get_by_id(db, Payment, payment_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payments require their dedicated reconciler",
            )
        lock_account(db, str(initial.account_id))
        payment = lock_for_update(db, Payment, initial.id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.settlement is not None:
            return payment.settlement
        if not payment.is_active or payment.status != PaymentStatus.succeeded:
            raise HTTPException(
                status_code=409,
                detail="Only an active historical succeeded payment can be reconciled",
            )
        if payment.refunds or payment.reversal is not None:
            raise HTTPException(
                status_code=409,
                detail="Refunded or reversed historical evidence requires review first",
            )
        allocations = [
            allocation for allocation in payment.allocations if allocation.is_active
        ]
        expected_allocation_ids = {allocation.id for allocation in allocations}
        if set(payload.allocation_ledger_entry_ids) != expected_allocation_ids:
            raise HTTPException(
                status_code=409,
                detail="Every active allocation requires one explicit ledger selection",
            )
        selected_ids = list(payload.allocation_ledger_entry_ids.values())
        if payload.unallocated_ledger_entry_id:
            selected_ids.append(payload.unallocated_ledger_entry_id)
        if payload.prepaid_ledger_entry_id:
            selected_ids.append(payload.prepaid_ledger_entry_id)
        if len(selected_ids) != len(set(selected_ids)):
            raise HTTPException(
                status_code=409, detail="A ledger entry cannot prove two effects"
            )

        def reviewed_entry(entry_id: UUID) -> LedgerEntry:
            entry = lock_for_update(db, LedgerEntry, entry_id)
            if entry is None or not entry.is_active:
                raise HTTPException(
                    status_code=409, detail="Selected ledger evidence is unavailable"
                )
            if entry.payment_id != payment.id or entry.currency != payment.currency:
                raise HTTPException(
                    status_code=409,
                    detail="Selected ledger evidence belongs to different money",
                )
            return entry

        allocation_total = Decimal("0.00")
        for allocation in allocations:
            entry = reviewed_entry(payload.allocation_ledger_entry_ids[allocation.id])
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            expected_account_id = invoice.account_id if invoice else payment.account_id
            expected_amount = round_money(to_decimal(allocation.amount))
            if (
                invoice is None
                or entry.account_id != expected_account_id
                or entry.invoice_id != allocation.invoice_id
                or entry.entry_type != LedgerEntryType.credit
                or entry.source != LedgerSource.payment
                or round_money(to_decimal(entry.amount)) != expected_amount
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected allocation ledger entry is not an exact match",
                )
            if (
                allocation.ledger_entry_id is not None
                and allocation.ledger_entry_id != entry.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Allocation already points to different evidence",
                )
            allocation.ledger_entry_id = entry.id
            allocation_total = round_money(allocation_total + expected_amount)

        payment_amount = round_money(to_decimal(payment.amount))
        unallocated_amount = round_money(payment_amount - allocation_total)
        if unallocated_amount < Decimal("0.00"):
            raise HTTPException(
                status_code=409, detail="Historical allocations exceed payment amount"
            )
        unallocated_entry: LedgerEntry | None = None
        if unallocated_amount > Decimal("0.00"):
            if payload.unallocated_ledger_entry_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="Unallocated payment remainder requires explicit evidence",
                )
            unallocated_entry = reviewed_entry(payload.unallocated_ledger_entry_id)
            if (
                unallocated_entry.account_id != payment.account_id
                or unallocated_entry.invoice_id is not None
                or unallocated_entry.entry_type != LedgerEntryType.credit
                or unallocated_entry.source != LedgerSource.payment
                or round_money(to_decimal(unallocated_entry.amount))
                != unallocated_amount
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected unallocated ledger entry is not an exact match",
                )
        elif payload.unallocated_ledger_entry_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Fully allocated payment cannot select unallocated evidence",
            )

        prepaid_entry: LedgerEntry | None = None
        if payload.prepaid_ledger_entry_id is not None:
            prepaid_entry = reviewed_entry(payload.prepaid_ledger_entry_id)
            if (
                prepaid_entry.account_id != payment.account_id
                or prepaid_entry.invoice_id is not None
                or prepaid_entry.entry_type != LedgerEntryType.debit
                or prepaid_entry.source != LedgerSource.invoice
                or round_money(to_decimal(prepaid_entry.amount)) > unallocated_amount
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Selected prepaid ledger entry is not an exact match",
                )

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "kind": "historical_payment_settlement_evidence",
                    "payment_id": str(payment.id),
                    "allocation_ledger_entry_ids": {
                        str(key): str(value)
                        for key, value in sorted(
                            payload.allocation_ledger_entry_ids.items(),
                            key=lambda item: str(item[0]),
                        )
                    },
                    "unallocated_ledger_entry_id": (
                        str(payload.unallocated_ledger_entry_id)
                        if payload.unallocated_ledger_entry_id
                        else None
                    ),
                    "prepaid_ledger_entry_id": (
                        str(payload.prepaid_ledger_entry_id)
                        if payload.prepaid_ledger_entry_id
                        else None
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        settlement = PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=(
                unallocated_entry.id if unallocated_entry else None
            ),
            prepaid_ledger_entry_id=(prepaid_entry.id if prepaid_entry else None),
            amount=payment_amount,
            unallocated_amount=unallocated_amount,
            prepaid_amount=(
                round_money(to_decimal(prepaid_entry.amount))
                if prepaid_entry
                else Decimal("0.00")
            ),
            currency=payment.currency,
            origin=PaymentSettlementOrigin.system,
            preview_fingerprint=fingerprint,
        )
        db.add(settlement)
        db.flush()
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action="reconcile_settlement_evidence",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata_={
                    "settlement_id": str(settlement.id),
                    "reason": payload.reason,
                    "preview_fingerprint": fingerprint,
                    "allocation_ledger_entry_ids": {
                        str(key): str(value)
                        for key, value in payload.allocation_ledger_entry_ids.items()
                    },
                    "unallocated_ledger_entry_id": (
                        str(unallocated_entry.id) if unallocated_entry else None
                    ),
                    "prepaid_ledger_entry_id": (
                        str(prepaid_entry.id) if prepaid_entry else None
                    ),
                    "money_posted": False,
                },
            ),
        )
        if commit:
            db.commit()
            db.refresh(settlement)
        else:
            db.flush()
        return settlement

    @staticmethod
    def edit_capability(db: Session, payment_id: str) -> PaymentEditCapability:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.refunds or payment.reversal is not None:
            return PaymentEditCapability(
                False,
                "Evidence-backed refund or reversal payments are immutable",
            )
        if payment.settlement is not None:
            return PaymentEditCapability(
                False,
                "Settled payment fields are immutable evidence",
            )
        return PaymentEditCapability(True, None)

    @staticmethod
    def _auto_allocate(db: Session, payment: Payment) -> list[PaymentAllocation]:
        """Auto-allocate payment to oldest unpaid invoices.

        For account-scoped payments, only the payer's own invoices are
        candidates. For consolidated (billing-account-scoped) payments,
        candidates span every subscriber under the billing account's reseller.

        Returns:
            List of created allocations
        """
        remaining = round_money(to_decimal(payment.amount))
        if remaining <= 0:
            return []
        invoice_query = (
            db.query(Invoice)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .filter(Invoice.balance_due > 0)
        )
        if payment.billing_account_id is not None:
            from app.models.billing import BillingAccount
            from app.models.subscriber import Subscriber

            invoice_query = invoice_query.join(
                Subscriber, Invoice.account_id == Subscriber.id
            ).filter(
                Subscriber.reseller_id
                == db.query(BillingAccount.reseller_id)
                .filter(BillingAccount.id == payment.billing_account_id)
                .scalar_subquery()
            )
        else:
            invoice_query = invoice_query.filter(
                Invoice.account_id == payment.account_id
            )
        invoices = invoice_query.order_by(
            Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc()
        ).all()
        allocations: list[PaymentAllocation] = []
        for invoice in invoices:
            if invoice.currency != payment.currency:
                continue
            amount = min(remaining, round_money(to_decimal(invoice.balance_due)))
            if amount <= 0:
                continue

            allocation, applied_amount = _apply_payment_allocation(
                db,
                payment,
                invoice,
                amount,
            )
            allocations.append(allocation)

            remaining = round_money(remaining - applied_amount)
            if remaining <= 0:
                break

        _record_unallocated_payment_credit(db, payment, remaining)
        apply_prepaid_service_credit(db, payment)

        return allocations

    @staticmethod
    def _create_allocations(
        db: Session,
        payment: Payment,
        allocations: list[PaymentAllocationCreate],
    ) -> list[PaymentAllocation]:
        """Create explicit allocations from payment to invoices.

        Args:
            db: Database session
            payment: The payment to allocate
            allocations: List of allocation specifications

        Returns:
            List of created allocations
        """
        created = []
        remaining = round_money(to_decimal(payment.amount))
        member_reseller_id: str | None = None
        if payment.billing_account_id is not None:
            from app.models.billing import BillingAccount

            ba = get_by_id(db, BillingAccount, payment.billing_account_id)
            if not ba:
                raise HTTPException(status_code=404, detail="Billing account not found")
            member_reseller_id = str(ba.reseller_id)
        for allocation in allocations:
            if allocation.amount > remaining:
                raise HTTPException(
                    status_code=400, detail="Allocation amount exceeds payment amount"
                )
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if payment.billing_account_id is not None:
                from app.models.subscriber import Subscriber

                subscriber = get_by_id(db, Subscriber, invoice.account_id)
                if (
                    subscriber is None
                    or str(subscriber.reseller_id) != member_reseller_id
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Invoice does not belong to a subscriber of this billing account's reseller",
                    )
            elif str(invoice.account_id) != str(payment.account_id):
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            _validate_invoice_currency(invoice, payment.currency)
            _assert_invoice_allocatable(invoice)

            # Cap the allocation at the invoice's outstanding balance so an
            # overpayment cannot over-allocate (allocations summing above the
            # invoice total). The uncapped surplus stays in ``remaining`` and is
            # credited to the account by _record_unallocated_payment_credit.
            invoice_balance = round_money(
                to_decimal(
                    invoice.balance_due
                    if invoice.balance_due is not None
                    else invoice.total
                )
            )
            if invoice_balance < 0:
                invoice_balance = Decimal("0.00")
            alloc_amount = min(
                round_money(to_decimal(allocation.amount)), invoice_balance
            )
            if alloc_amount <= 0:
                # Invoice already settled; leave the amount in ``remaining`` so
                # it is credited as account balance below.
                continue

            entry, applied_amount = _apply_payment_allocation(
                db,
                payment,
                invoice,
                alloc_amount,
                memo=allocation.memo,
            )
            created.append(entry)

            remaining = round_money(remaining - applied_amount)

        _record_unallocated_payment_credit(db, payment, remaining)

        return created

    @staticmethod
    def create(
        db: Session,
        payload: PaymentCreate,
        *,
        auto_allocate: bool = True,
        commit: bool = True,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.system,
    ):
        """Create a payment.

        When ``auto_allocate`` is False and no explicit allocations are given,
        the payment is NOT spread over open invoices; the full amount is
        recorded as unallocated account credit instead. Default behavior
        (auto-allocate to oldest unpaid invoices) is unchanged.

        ``commit=False`` posts the payment on the caller's transaction and
        flushes instead of committing, so a caller that is already inside a
        SAVEPOINT (the bulk import wizard, which isolates each row so one bad
        row cannot roll back the batch) can still route through this owner
        rather than hand-rolling a Payment row. The caller owns the commit.
        """
        if payload.amount is not None and payload.amount <= 0:
            raise HTTPException(
                status_code=400, detail="Payment amount must be greater than 0"
            )
        # Double-submit guard for manually recorded payments. Gateway payments
        # are deduped by the uq_payments_active_external_id partial index, but a
        # manual/offline payment has no external_id/provider_id, so a
        # double-clicked "record payment" would create two rows and over-credit
        # the account. Reject an identical manual payment recorded in the last
        # minute so a rapid retry cannot create a second customer payment. (#29)
        if (
            payload.external_id is None
            and payload.provider_id is None
            and payload.amount is not None
        ):
            scope_col, scope_val = (
                (Payment.account_id, payload.account_id)
                if payload.account_id is not None
                else (Payment.billing_account_id, payload.billing_account_id)
            )
            if scope_val is not None:
                duplicate = (
                    db.query(Payment.id)
                    .filter(
                        scope_col == scope_val,
                        Payment.amount == payload.amount,
                        Payment.external_id.is_(None),
                        Payment.provider_id.is_(None),
                        Payment.is_active.is_(True),
                        Payment.created_at >= datetime.now(UTC) - timedelta(seconds=60),
                    )
                    .first()
                )
                if duplicate:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "An identical payment was just recorded moments ago. "
                            "Refresh the page to confirm it before recording again."
                        ),
                    )
        data = payload.model_dump(exclude={"allocations"})
        fields_set = payload.model_fields_set
        if "currency" not in fields_set:
            default_currency = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_currency"
            )
            if default_currency:
                data["currency"] = default_currency
        if "status" not in fields_set:
            default_status = settings_spec.resolve_value(
                db, SettingDomain.billing, "default_payment_status"
            )
            if default_status:
                data["status"] = validate_enum(default_status, PaymentStatus, "status")
        if data.get("status") in {
            PaymentStatus.refunded,
            PaymentStatus.partially_refunded,
            PaymentStatus.reversed,
        }:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Refunded and reversed payment states require their exact "
                    "owner evidence workflow"
                ),
            )
        if payload.account_id is not None:
            _validate_payment_linkages(
                db,
                str(payload.account_id),
                None,
                str(payload.payment_method_id) if payload.payment_method_id else None,
            )
        elif payload.billing_account_id is not None:
            from app.services.billing.billing_accounts import BillingAccounts

            BillingAccounts.get(db, str(payload.billing_account_id))
        _validate_payment_provider(
            db, str(payload.provider_id) if payload.provider_id else None
        )
        channel = _resolve_payment_channel(
            db,
            str(payload.payment_channel_id) if payload.payment_channel_id else None,
            str(payload.payment_method_id) if payload.payment_method_id else None,
            str(payload.provider_id) if payload.provider_id else None,
        )
        if channel and not payload.payment_channel_id:
            data["payment_channel_id"] = channel.id
        collection_account = _resolve_collection_account(
            db,
            channel,
            data.get("currency"),
            str(payload.collection_account_id)
            if payload.collection_account_id
            else None,
        )
        if collection_account and not payload.collection_account_id:
            data["collection_account_id"] = collection_account.id
        if payload.collection_account_id and not collection_account:
            _validate_collection_account(
                db, str(payload.collection_account_id), data.get("currency")
            )
        # Validate allocation invoices against payment currency
        if payload.allocations:
            for alloc in payload.allocations:
                invoice = get_by_id(db, Invoice, alloc.invoice_id)
                if invoice:
                    _validate_invoice_currency(invoice, data.get("currency"))
                    _assert_invoice_allocatable(invoice)
        # A payment created already in the succeeded state must carry paid_at.
        # Gateway/top-up reconciliation (e.g. Paystack top-ups via
        # reconcile_topups) creates succeeded payments without an explicit
        # paid_at; without it, billing-enforcement health (which counts recent
        # settlements by paid_at) goes blind and blocks all collections
        # suspensions. Stamp it here so every caller is covered.
        if data.get("status") == PaymentStatus.succeeded and not data.get("paid_at"):
            data["paid_at"] = datetime.now(UTC)
        normalized_payload = PaymentCreate(
            **data,
            allocations=payload.allocations,
        )
        if normalized_payload.account_id is not None:
            if normalized_payload.status in {
                PaymentStatus.pending,
                PaymentStatus.succeeded,
            }:
                preview = _build_payment_creation_preview(
                    db,
                    normalized_payload,
                    auto_allocate=auto_allocate,
                )
                return _create_account_payment_from_preview(
                    db,
                    normalized_payload,
                    preview,
                    auto_allocate=auto_allocate,
                    origin=origin,
                    idempotency_key=None,
                    commit=commit,
                ).payment
            if normalized_payload.allocations:
                raise HTTPException(
                    status_code=409,
                    detail="Unsettled failed/canceled payments cannot carry allocations",
                )
            payment = Payment(**data)
            db.add(payment)
            db.flush()
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.system,
                    action="record_payment_observation",
                    entity_type="payment",
                    entity_id=str(payment.id),
                    metadata_={
                        "amount": str(payment.amount),
                        "currency": payment.currency,
                        "status": payment.status.value,
                        "money_effect": "none",
                        "access_consequence": "none",
                    },
                ),
            )
            if commit:
                db.commit()
                db.refresh(payment)
            else:
                db.flush()
            return payment

        if normalized_payload.status == PaymentStatus.succeeded:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Confirmed consolidated payments require the dedicated "
                    "preview and settlement owner"
                ),
            )
        if normalized_payload.allocations:
            raise HTTPException(
                status_code=409,
                detail="Unsettled consolidated payments cannot carry allocations",
            )
        data["auto_allocate_on_settlement"] = auto_allocate
        payment = Payment(**data)
        db.add(payment)
        db.flush()
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action="record_consolidated_payment_observation",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata_={
                    "billing_account_id": str(payment.billing_account_id),
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "status": payment.status.value,
                    "money_effect": "none",
                    "service_access_consequence": "none",
                },
            ),
        )
        if commit:
            db.commit()
            db.refresh(payment)
        else:
            db.flush()
        return payment

    @staticmethod
    def allocate_consolidated_balance_to_subscriber(
        db: Session,
        billing_account_id: str,
        subscriber_id: str,
        amount: Decimal | int | float | str | None = None,
    ) -> dict:
        """Allocate a reseller billing account's unallocated balance to one subscriber.

        The credit is consumed from the billing account's existing unallocated
        consolidated payments, oldest first, and applied to the selected
        subscriber's oldest open invoices.
        """
        from app.models.billing import BillingAccount
        from app.models.subscriber import Subscriber
        from app.services.billing.billing_accounts import BillingAccounts

        ba = (
            db.query(BillingAccount)
            .filter(BillingAccount.id == billing_account_id)
            .with_for_update()
            .first()
        )
        if not ba:
            raise HTTPException(status_code=404, detail="Billing account not found")
        available_balance = round_money(to_decimal(ba.balance))
        if available_balance <= 0:
            raise HTTPException(
                status_code=400, detail="No unallocated reseller funds available"
            )
        allocation_limit = available_balance
        if amount is not None:
            allocation_limit = round_money(to_decimal(amount))
            if allocation_limit <= 0:
                raise HTTPException(
                    status_code=400, detail="Allocation amount must be greater than 0"
                )
            if allocation_limit > available_balance:
                raise HTTPException(
                    status_code=400,
                    detail="Allocation exceeds unallocated reseller funds",
                )

        subscriber = get_by_id(db, Subscriber, subscriber_id)
        if subscriber is None or str(subscriber.reseller_id) != str(ba.reseller_id):
            raise HTTPException(status_code=404, detail="Subscriber not found")

        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id == subscriber.id)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [
                        InvoiceStatus.issued,
                        InvoiceStatus.partially_paid,
                        InvoiceStatus.overdue,
                    ]
                )
            )
            .filter(Invoice.balance_due > 0)
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
        if not invoices:
            raise HTTPException(
                status_code=400, detail="Subscriber has no open invoices"
            )

        allocated_sq = (
            db.query(
                PaymentAllocation.payment_id.label("payment_id"),
                func.coalesce(
                    func.sum(PaymentAllocation.amount), Decimal("0.00")
                ).label("allocated"),
            )
            .group_by(PaymentAllocation.payment_id)
            .subquery()
        )
        payment_result_rows = (
            db.query(
                Payment,
                func.coalesce(allocated_sq.c.allocated, Decimal("0.00")).label(
                    "allocated"
                ),
            )
            .outerjoin(allocated_sq, allocated_sq.c.payment_id == Payment.id)
            .filter(Payment.billing_account_id == ba.id)
            .filter(Payment.is_active.is_(True))
            .filter(Payment.status == PaymentStatus.succeeded)
            .order_by(Payment.paid_at.asc().nulls_last(), Payment.created_at.asc())
            .all()
        )
        payment_rows: list[tuple[Payment, Decimal]] = [
            (payment, cast(Decimal, allocated))
            for payment, allocated in payment_result_rows
        ]
        payment_backing_available = round_money(
            sum(
                (
                    round_money(to_decimal(payment.amount) - to_decimal(allocated))
                    for payment, allocated in payment_rows
                    if payment.currency == ba.currency
                ),
                Decimal("0.00"),
            )
        )
        if payment_backing_available < allocation_limit:
            backing_amount = round_money(allocation_limit - payment_backing_available)
            backing_payment = Payment(
                billing_account_id=ba.id,
                amount=backing_amount,
                currency=ba.currency,
                status=PaymentStatus.succeeded,
                memo="Reseller unallocated balance credit",
                paid_at=datetime.now(UTC),
            )
            db.add(backing_payment)
            db.flush()
            payment_rows.append((backing_payment, Decimal("0.00")))

        remaining_balance = allocation_limit
        total_allocated = Decimal("0.00")
        invoice_ids: set = set()
        allocations_by_payment: dict[Payment, list[PaymentAllocation]] = {}
        payment_remaining_by_id = {
            payment.id: round_money(to_decimal(payment.amount) - to_decimal(allocated))
            for payment, allocated in payment_rows
        }

        for invoice in invoices:
            invoice_remaining = round_money(to_decimal(invoice.balance_due))
            if invoice_remaining <= 0:
                continue

            for payment, _already_allocated in payment_rows:
                if remaining_balance <= 0 or invoice_remaining <= 0:
                    break
                if payment.currency != invoice.currency:
                    continue

                payment_available = payment_remaining_by_id.get(
                    payment.id, Decimal("0.00")
                )
                if payment_available <= 0:
                    continue

                amount = min(remaining_balance, invoice_remaining, payment_available)
                allocation, applied_amount = _apply_payment_allocation(
                    db,
                    payment,
                    invoice,
                    amount,
                    memo="Allocated from reseller unallocated funds",
                )
                allocations_by_payment.setdefault(payment, []).append(allocation)
                total_allocated = round_money(total_allocated + applied_amount)
                remaining_balance = round_money(remaining_balance - applied_amount)
                invoice_remaining = round_money(invoice_remaining - applied_amount)
                payment_remaining_by_id[payment.id] = round_money(
                    payment_available - applied_amount
                )
                invoice_ids.add(invoice.id)

            if remaining_balance <= 0:
                break

        if total_allocated <= 0:
            raise HTTPException(
                status_code=400,
                detail="No eligible unallocated reseller funds could be applied",
            )
        if total_allocated > available_balance:
            raise HTTPException(
                status_code=400, detail="Allocation exceeds unallocated reseller funds"
            )

        db.flush()
        for invoice_id in invoice_ids:
            recalculated_invoice = get_by_id(db, Invoice, invoice_id)
            if recalculated_invoice:
                _finalize_invoice_payment_effects(db, recalculated_invoice)

        BillingAccounts.debit_balance(db, str(ba.id), total_allocated)
        db.commit()

        for payment, allocations in allocations_by_payment.items():
            _emit_consolidated_payment_events(db, payment, allocations)

        return {
            "subscriber_id": str(subscriber.id),
            "allocated_total": total_allocated,
            "currency": ba.currency,
            "remaining_unallocated_balance": round_money(
                available_balance - total_allocated
            ),
            "invoice_count": len(invoice_ids),
        }

    @staticmethod
    def get(db: Session, payment_id: str):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return payment

    @staticmethod
    def list(
        db: Session,
        account_id: str | None,
        invoice_id: str | None,
        status: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
        *,
        updated_since: datetime | None = None,
    ):
        query = db.query(Payment)
        if account_id:
            query = query.filter(Payment.account_id == account_id)
        if invoice_id:
            query = query.join(
                PaymentAllocation, PaymentAllocation.payment_id == Payment.id
            ).filter(PaymentAllocation.invoice_id == invoice_id)
        if status:
            query = query.filter(
                Payment.status == validate_enum(status, PaymentStatus, "status")
            )
        if is_active is None:
            query = query.filter(Payment.is_active.is_(True))
        else:
            query = query.filter(Payment.is_active == is_active)
        # Incremental-sync watermark (see Invoices.list); backed by
        # ix_payments_is_active_updated_at.
        if updated_since is not None:
            query = query.filter(Payment.updated_at >= updated_since)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": Payment.created_at,
                "updated_at": Payment.updated_at,
                "paid_at": Payment.paid_at,
                "status": Payment.status,
            },
        )
        # Stable, keyset-friendly tiebreaker for deterministic forward paging.
        query = query.order_by(Payment.id.asc())
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_sync(
        db: Session,
        *,
        account_id: str | None,
        status: str | None,
        is_active: bool | None,
        updated_since: datetime | None,
        limit: int,
        offset: int,
    ):
        query = db.query(Payment).options(
            selectinload(
                Payment.allocations.and_(PaymentAllocation.is_active.is_(True))
            ),
            selectinload(Payment.withholding_tax_record),
        )
        if account_id:
            query = query.filter(Payment.account_id == account_id)
        if status:
            query = query.filter(
                Payment.status == validate_enum(status, PaymentStatus, "status")
            )
        if is_active is None:
            query = query.filter(Payment.is_active.is_(True))
        else:
            query = query.filter(Payment.is_active == is_active)
        return apply_sync_page(
            query,
            Payment,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=kwargs["limit"], offset=kwargs["offset"])

    @staticmethod
    def update(db: Session, payment_id: str, payload: PaymentUpdate):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        data = payload.model_dump(exclude_unset=True)
        if payment.settlement is not None:
            forbidden = set(data) - {"memo"}
            if forbidden:
                raise HTTPException(
                    status_code=409,
                    detail="Settled payment fields are immutable evidence",
                )
        if payment.refunds or payment.reversal is not None:
            forbidden = set(data) - {"memo"}
            if forbidden:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Evidence-backed refund or reversal payments cannot be "
                        "changed by the generic editor"
                    ),
                )
        if "account_id" in data or "billing_account_id" in data:
            raise HTTPException(
                status_code=400,
                detail="Payment scope (account_id / billing_account_id) cannot be changed after creation",
            )
        effective_account_id = data.get("account_id", payment.account_id)
        payment_method_id = data.get("payment_method_id", payment.payment_method_id)
        explicit_channel = "payment_channel_id" in data
        payment_channel_id = (
            data.get("payment_channel_id") if explicit_channel else None
        )
        collection_account_id = data.get(
            "collection_account_id", payment.collection_account_id
        )
        provider_id = data.get("provider_id", payment.provider_id)
        if effective_account_id is not None:
            _validate_payment_linkages(
                db,
                str(effective_account_id),
                None,
                str(payment_method_id) if payment_method_id else None,
            )
        _validate_payment_provider(db, str(provider_id) if provider_id else None)
        channel = _resolve_payment_channel(
            db,
            str(payment_channel_id) if payment_channel_id else None,
            str(payment_method_id) if payment_method_id else None,
            str(provider_id) if provider_id else None,
        )
        if channel and not explicit_channel:
            data["payment_channel_id"] = channel.id
        collection_account = _resolve_collection_account(
            db,
            channel,
            data.get("currency", payment.currency),
            str(collection_account_id) if collection_account_id else None,
        )
        if collection_account and not collection_account_id:
            data["collection_account_id"] = collection_account.id
        if collection_account_id and not collection_account:
            _validate_collection_account(
                db, str(collection_account_id), data.get("currency", payment.currency)
            )
        # A status change is a settlement decision, not a field edit. mark_status
        # owns it: it enforces the legal-transition table, stamps paid_at (a
        # succeeded payment with a NULL paid_at is invisible to the enforcement
        # health gate and silently blocks all collections suspensions), resolves
        # dunning cases, applies prepaid service credit, and emits
        # payment_received. Blind-setattr here bypassed every one of those and
        # reopened the production paid_at regression that create() already fixed.
        requested_status = data.pop("status", None)
        if requested_status is not None:
            normalized_status = validate_enum(requested_status, PaymentStatus, "status")
            if normalized_status in {
                PaymentStatus.refunded,
                PaymentStatus.partially_refunded,
                PaymentStatus.reversed,
            }:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Refunded and reversed states are owned by exact "
                        "financial-action workflows; preview and confirm them"
                    ),
                )
            if (
                normalized_status == PaymentStatus.succeeded
                and payment.status != PaymentStatus.succeeded
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Payment settlement requires preview and confirmation through "
                        "the settlement owner"
                    ),
                )

        for key, value in data.items():
            setattr(payment, key, value)
        invoice_ids = [alloc.invoice_id for alloc in payment.allocations]
        for invoice_id in invoice_ids:
            invoice = get_by_id(db, Invoice, invoice_id)
            if invoice:
                db.flush()
                _finalize_invoice_payment_effects(db, invoice)
        db.commit()

        if requested_status is not None:
            normalized = validate_enum(requested_status, PaymentStatus, "status")
            if normalized and normalized != payment.status:
                Payments.mark_status(db, str(payment.id), normalized)

        db.refresh(payment)
        return payment

    @staticmethod
    def reallocate(db: Session, payment_id: str, invoice_id: str) -> Payment:
        """Move a payment's allocation to a different invoice.

        The canonical owner operation for "admin pointed this payment at the
        wrong invoice". It must be used instead of rewriting ``PaymentAllocation``
        rows directly: the money's effect is spread across the allocation, the
        ledger credit, and the *two* invoices' derived totals, and all of them
        have to move together.

        For each invoice released, the payment ledger credit is deactivated and
        the invoice is recomputed, so a released invoice cannot keep reading as
        ``paid`` with no money behind it. The new allocation is then capped at
        the target's ``balance_due`` — an over-payment becomes account credit
        rather than an allocation larger than the debt it settles.
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.refunds or payment.reversal is not None:
            raise HTTPException(
                status_code=409,
                detail=("Evidence-backed refund or reversal allocations are immutable"),
            )
        if payment.settlement is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Evidence-backed allocations cannot be reallocated; use a "
                    "reviewed reversal workflow"
                ),
            )
        if not payment.is_active:
            raise HTTPException(
                status_code=409, detail="Inactive payment cannot be reallocated"
            )
        if payment.status != PaymentStatus.succeeded:
            raise HTTPException(
                status_code=409,
                detail="Only a succeeded payment can be reallocated",
            )
        if payment.account_id is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Consolidated payments require their dedicated allocation "
                    "workflow and cannot be reallocated here"
                ),
            )

        # Account lock first: every subscriber-scoped payment and invoice in
        # this operation shares that serialization key. Then lock the payment
        # and target rows so a concurrent edit cannot change either underneath
        # the release-and-apply sequence.
        lock_account(db, str(payment.account_id))
        payment = (
            db.query(Payment).filter(Payment.id == payment.id).with_for_update().one()
        )
        target = (
            db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
        )
        if not target or not target.is_active:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(target.account_id) != str(payment.account_id):
            raise HTTPException(
                status_code=400,
                detail="Invoice belongs to a different account than the payment",
            )
        _validate_invoice_currency(target, payment.currency)
        _assert_invoice_allocatable(target)
        if target.status == InvoiceStatus.written_off:
            raise HTTPException(
                status_code=400,
                detail="Cannot allocate a payment to a written_off invoice",
            )

        already_on_target = any(
            allocation.is_active and str(allocation.invoice_id) == str(target.id)
            for allocation in payment.allocations
        )
        if not already_on_target and round_money(
            to_decimal(target.balance_due or Decimal("0.00"))
        ) <= Decimal("0.00"):
            raise HTTPException(status_code=400, detail="Invoice has no balance due")

        released: list[Invoice] = []
        for allocation in list(payment.allocations):
            if not allocation.is_active:
                continue
            if str(allocation.invoice_id) == str(target.id):
                # Already where it needs to be; nothing to move.
                continue
            previous = get_by_id(db, Invoice, allocation.invoice_id)
            # Mirror PaymentAllocations.delete: drop the ledger credit with the
            # allocation, never one without the other.
            db.query(LedgerEntry).filter(
                LedgerEntry.payment_id == allocation.payment_id,
                LedgerEntry.invoice_id == allocation.invoice_id,
                LedgerEntry.source == LedgerSource.payment,
            ).update({"is_active": False})
            allocation.is_active = False
            if previous is not None:
                released.append(previous)

        db.flush()
        for previous in released:
            _finalize_invoice_payment_effects(db, previous)

        db.flush()
        db.refresh(target)

        # Only the part of the payment that is not already allocated is free to
        # move. Without this, re-pointing a payment at the invoice it is already
        # allocated to would treat the whole amount as surplus and mint credit
        # the customer never paid.
        allocated = sum(
            (
                round_money(to_decimal(a.amount))
                for a in payment.allocations
                if a.is_active
            ),
            Decimal("0.00"),
        )
        amount = round_money(to_decimal(payment.amount))
        unallocated = amount - allocated
        if unallocated <= 0:
            db.commit()
            db.refresh(payment)
            return payment

        payable = round_money(to_decimal(target.balance_due or Decimal("0.00")))
        applied = min(unallocated, payable) if payable > 0 else Decimal("0.00")

        if applied > 0:
            _apply_payment_allocation(
                db,
                payment,
                target,
                applied,
                memo=f"Reallocated from payment {payment.id}",
            )
            db.flush()
            _finalize_invoice_payment_effects(db, target)

        # Anything the target could not absorb stays with the customer as credit
        # instead of silently inflating the allocation.
        _record_unallocated_payment_credit(db, payment, unallocated - applied)

        db.commit()
        db.refresh(payment)
        return payment

    @staticmethod
    def delete(db: Session, payment_id: str):
        """Soft-delete a payment and remove every effect it had.

        A payment's effect is spread across the payment row, its allocations, and
        the ledger credits those allocations justify. Deactivating only the payment
        row left the ledger credit ACTIVE and unallocated — so the money kept
        counting toward the customer's spendable credit even though the payment
        that created it was gone.

        Allocation and ledger credit drop together, the same way
        ``PaymentAllocations.delete`` and ``Payments.reallocate`` do it. Never one
        without the other.
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.refunds or payment.reversal is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Evidence-backed refund or reversal payments cannot be deleted"
                ),
            )
        if payment.settlement is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Evidence-backed settled payment cannot be deleted; use a "
                    "reviewed reversal workflow"
                ),
            )

        invoices = [
            invoice
            for allocation in payment.allocations
            if allocation.is_active
            and (invoice := get_by_id(db, Invoice, allocation.invoice_id))
        ]

        # Drop every ledger entry this payment posted — the invoice-scoped credits
        # AND the unallocated surplus credit, which nothing else would ever undo.
        db.query(LedgerEntry).filter(
            LedgerEntry.payment_id == payment.id,
            LedgerEntry.is_active.is_(True),
        ).update({"is_active": False}, synchronize_session=False)

        for allocation in payment.allocations:
            allocation.is_active = False

        payment.is_active = False
        db.flush()

        for invoice in invoices:
            _finalize_invoice_payment_effects(db, invoice)
        db.commit()

    @staticmethod
    def mark_status(
        db: Session,
        payment_id: str,
        status: PaymentStatus | str,
        *,
        origin: PaymentSettlementOrigin = PaymentSettlementOrigin.system,
    ):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        previous_status = payment.status
        normalized = validate_enum(status, PaymentStatus, "status")
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid status")
        # Refund and reversal are money movements, never generic lifecycle edits.
        if normalized in {
            PaymentStatus.refunded,
            PaymentStatus.partially_refunded,
            PaymentStatus.reversed,
        }:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Refunded and reversed states require their exact owner "
                    "evidence workflow"
                ),
            )
        # Guard against out-of-order / replayed gateway webhooks regressing
        # committed financial state (e.g. success after refunded, late failed
        # after success). An illegal transition is a no-op that still returns
        # the payment so the webhook gets a 200 and stops retrying.
        if (
            previous_status != normalized
            and normalized
            not in _ALLOWED_PAYMENT_TRANSITIONS.get(previous_status, set())
        ):
            logger.warning(
                "Ignoring illegal payment transition %s -> %s for payment %s",
                previous_status.value if previous_status else None,
                normalized.value,
                payment_id,
            )
            return payment

        if normalized == PaymentStatus.succeeded and (
            previous_status != PaymentStatus.succeeded
        ):
            return Payments.settle(
                db,
                payment_id,
                origin=origin,
            ).payment

        payment.status = normalized
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                db.flush()
                _finalize_invoice_payment_effects(db, invoice)
        db.commit()
        db.refresh(payment)

        # Emit payment event based on status transition
        if previous_status != normalized:
            allocation_invoice_id = _primary_allocation_invoice_id(payment)
            payload = {
                "payment_id": str(payment.id),
                "amount": str(payment.amount) if payment.amount else None,
                "currency": payment.currency,
                "invoice_id": allocation_invoice_id,
                "from_status": previous_status.value if previous_status else None,
                "to_status": normalized.value,
            }
            if normalized == PaymentStatus.failed:
                emit_event(
                    db,
                    EventType.payment_failed,
                    payload,
                    account_id=payment.account_id,
                    invoice_id=allocation_invoice_id,
                )
            elif normalized == PaymentStatus.refunded:
                emit_event(
                    db,
                    EventType.payment_refunded,
                    payload,
                    account_id=payment.account_id,
                    invoice_id=allocation_invoice_id,
                )
            # Persist the inline payment_received handlers' resolve/restore work
            # (run with commit=False); the payment is already committed above.
            db.commit()

        return payment


def _payment_unallocated_credit_remaining(
    db: Session,
    payment: Payment,
) -> Decimal:
    if payment.settlement is None:
        return Decimal("0.00")
    consumed = db.query(
        func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00"))
    ).join(
        PaymentAllocation,
        PaymentAllocation.consumption_ledger_entry_id == LedgerEntry.id,
    ).filter(PaymentAllocation.payment_id == payment.id).filter(
        PaymentAllocation.is_active.is_(True)
    ).filter(LedgerEntry.is_active.is_(True)).scalar() or Decimal("0.00")
    return max(
        Decimal("0.00"),
        round_money(
            to_decimal(payment.settlement.unallocated_amount)
            - to_decimal(payment.settlement.prepaid_amount)
            - to_decimal(consumed)
        ),
    )


def _payment_allocation_fingerprint(
    *,
    payment: Payment,
    settlement: PaymentSettlement,
    invoice: Invoice,
    amount: Decimal,
    funding_before: Decimal,
    payment_unallocated_before: Decimal,
    account_credit_before: Decimal,
    receivable_before: Decimal,
) -> str:
    encoded = json.dumps(
        {
            "kind": "payment_allocation",
            "payment_id": str(payment.id),
            "settlement_id": str(settlement.id),
            "invoice_id": str(invoice.id),
            "amount": f"{amount:.2f}",
            "currency": payment.currency,
            "prepaid_funding_before": f"{funding_before:.2f}",
            "payment_unallocated_before": f"{payment_unallocated_before:.2f}",
            "account_credit_before": f"{account_credit_before:.2f}",
            "receivable_before": f"{receivable_before:.2f}",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_payment_allocation_preview(
    db: Session,
    payload: PaymentAllocationPreviewRequest,
) -> PaymentAllocationPreview:
    payment = get_by_id(db, Payment, payload.payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if not payment.is_active or payment.status != PaymentStatus.succeeded:
        raise HTTPException(
            status_code=409,
            detail="Only an active, succeeded payment can fund an allocation",
        )
    if payment.account_id is None:
        raise HTTPException(
            status_code=409,
            detail="Consolidated payments require their dedicated allocation owner",
        )
    if payment.refunds or payment.reversal is not None:
        raise HTTPException(
            status_code=409,
            detail="Refunded or reversed payment allocations are immutable",
        )
    settlement = payment.settlement
    if settlement is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Payment has no reviewed settlement evidence; reconcile it before "
                "allocating account credit"
            ),
        )
    invoice = get_by_id(db, Invoice, payload.invoice_id)
    if not invoice or not invoice.is_active:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.account_id != payment.account_id:
        raise HTTPException(
            status_code=400, detail="Invoice does not belong to payment account"
        )
    _validate_invoice_currency(invoice, payment.currency)
    _assert_invoice_allocatable(invoice)
    existing = (
        db.query(PaymentAllocation.id)
        .filter(PaymentAllocation.payment_id == payment.id)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Payment already has allocation evidence for this invoice",
        )
    amount = round_money(to_decimal(payload.amount))
    if amount <= Decimal("0.00"):
        raise HTTPException(
            status_code=400, detail="Allocation amount must be positive"
        )
    receivable_before = round_money(to_decimal(invoice.balance_due))
    if amount > receivable_before:
        raise HTTPException(
            status_code=409,
            detail="Allocation exceeds the current invoice receivable",
        )
    payment_unallocated_before = _payment_unallocated_credit_remaining(db, payment)
    if amount > payment_unallocated_before:
        raise HTTPException(
            status_code=409,
            detail="Allocation exceeds this payment's unallocated credit",
        )
    account_credit_before = get_account_credit_balance(
        db, str(payment.account_id), currency=payment.currency
    )
    if amount > account_credit_before:
        raise HTTPException(
            status_code=409,
            detail="Allocation exceeds the account's currently available credit",
        )
    funding_before = calculate_customer_balance(
        db, payment.account_id, currency=payment.currency
    )
    fingerprint = _payment_allocation_fingerprint(
        payment=payment,
        settlement=settlement,
        invoice=invoice,
        amount=amount,
        funding_before=funding_before,
        payment_unallocated_before=payment_unallocated_before,
        account_credit_before=account_credit_before,
        receivable_before=receivable_before,
    )
    return PaymentAllocationPreview(
        payment_id=payment.id,
        settlement_id=settlement.id,
        invoice_id=invoice.id,
        invoice_number=invoice.invoice_number,
        amount=amount,
        currency=payment.currency,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_before,
        payment_unallocated_before=payment_unallocated_before,
        payment_unallocated_after=round_money(payment_unallocated_before - amount),
        account_credit_before=account_credit_before,
        account_credit_after=round_money(account_credit_before - amount),
        receivable_before=receivable_before,
        receivable_after=round_money(receivable_before - amount),
        invoice_ledger_entry_type=LedgerEntryType.credit,
        invoice_ledger_source=LedgerSource.payment,
        account_credit_ledger_entry_type=LedgerEntryType.debit,
        account_credit_ledger_source=LedgerSource.other,
        access_consequence="recheck_after_receivable_allocation",
        fingerprint=fingerprint,
    )


def _normalize_payment_allocation_key(value: str) -> str:
    key = value.strip()
    if not _REFUND_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail="Payment allocation idempotency key must be 16-120 safe characters",
        )
    return key


class PaymentAllocations(ListResponseMixin):
    @staticmethod
    def available_amount(db: Session, payment_id: str) -> Decimal:
        """Return owner-derived settled credit still eligible for allocation."""
        payment = get_by_id(db, Payment, payment_id)
        if (
            payment is None
            or not payment.is_active
            or payment.account_id is None
            or payment.status != PaymentStatus.succeeded
            or payment.settlement is None
            or payment.refunds
            or payment.reversal is not None
        ):
            return Decimal("0.00")
        payment_available = _payment_unallocated_credit_remaining(db, payment)
        account_available = get_account_credit_balance(
            db, str(payment.account_id), currency=payment.currency
        )
        return max(
            Decimal("0.00"),
            min(payment_available, round_money(account_available)),
        )

    @staticmethod
    def preview(
        db: Session,
        payload: PaymentAllocationPreviewRequest,
    ) -> PaymentAllocationPreview:
        return _build_payment_allocation_preview(db, payload)

    @staticmethod
    def _replay(
        db: Session,
        *,
        key: str,
        fingerprint: str,
    ) -> PaymentAllocationResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _PAYMENT_ALLOCATION_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(status_code=409, detail="Allocation is being recorded")
        allocation = get_by_id(db, PaymentAllocation, reservation.ref_id)
        if not allocation:
            raise HTTPException(
                status_code=409, detail="Payment allocation evidence is incomplete"
            )
        if allocation.preview_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different allocation preview",
            )
        return PaymentAllocationResult(
            allocation=allocation,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def confirm(
        db: Session,
        payload: PaymentAllocationConfirm,
        *,
        commit: bool = True,
    ) -> PaymentAllocationResult:
        key = _normalize_payment_allocation_key(payload.idempotency_key)
        replay = PaymentAllocations._replay(
            db, key=key, fingerprint=payload.preview_fingerprint
        )
        if replay:
            return replay
        payment = get_by_id(db, Payment, payload.payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payments require their dedicated allocation owner",
            )
        lock_account(db, str(payment.account_id))
        payment = lock_for_update(db, Payment, payment.id)
        invoice = lock_for_update(db, Invoice, payload.invoice_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        preview_request = PaymentAllocationPreviewRequest(
            payment_id=payload.payment_id,
            invoice_id=payload.invoice_id,
            amount=payload.amount,
        )
        preview = _build_payment_allocation_preview(db, preview_request)
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = PaymentAllocations._replay(
            db, key=key, fingerprint=payload.preview_fingerprint
        )
        if replay:
            return replay
        reservation = IdempotencyKey(
            scope=_PAYMENT_ALLOCATION_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=payment.account_id,
        )
        db.add(reservation)
        try:
            allocation = PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=preview.amount,
                memo=f"Allocated account credit from payment {payment.id}",
                preview_fingerprint=preview.fingerprint,
                idempotency_key=key,
            )
            db.add(allocation)
            invoice_entry = _create_payment_ledger_entry(
                db, payment, invoice, preview.amount
            )
            if invoice_entry is None:
                raise HTTPException(
                    status_code=409,
                    detail="Invoice allocation ledger evidence could not be created",
                )
            consumption_entry = LedgerEntry(
                account_id=payment.account_id,
                invoice_id=None,
                payment_id=payment.id,
                entry_type=LedgerEntryType.debit,
                source=LedgerSource.other,
                amount=preview.amount,
                currency=payment.currency,
                memo=(f"{_PAYMENT_ALLOCATION_CONSUMPTION_MEMO_PREFIX} {invoice.id}"),
            )
            db.add(consumption_entry)
            db.flush()
            allocation.ledger_entry_id = invoice_entry.id
            allocation.consumption_ledger_entry_id = consumption_entry.id
            reservation.ref_id = str(allocation.id)
            _finalize_invoice_payment_effects(db, invoice)
            AuditEvents.stage(
                db,
                AuditEventCreate(
                    actor_type=AuditActorType.system,
                    action="allocate_payment_credit",
                    entity_type="payment_allocation",
                    entity_id=str(allocation.id),
                    metadata_={
                        "payment_id": str(payment.id),
                        "settlement_id": str(preview.settlement_id),
                        "invoice_id": str(invoice.id),
                        "amount": str(preview.amount),
                        "currency": preview.currency,
                        "preview_fingerprint": preview.fingerprint,
                        "invoice_ledger_entry_id": str(invoice_entry.id),
                        "account_credit_consumption_ledger_entry_id": str(
                            consumption_entry.id
                        ),
                        "prepaid_funding_before": str(preview.prepaid_funding_before),
                        "prepaid_funding_after": str(preview.prepaid_funding_after),
                        "account_credit_before": str(preview.account_credit_before),
                        "account_credit_after": str(preview.account_credit_after),
                        "receivable_before": str(preview.receivable_before),
                        "receivable_after": str(preview.receivable_after),
                        "access_consequence": preview.access_consequence,
                    },
                ),
            )
            db.flush()
            if commit:
                db.commit()
                db.refresh(allocation)
            return PaymentAllocationResult(
                allocation=allocation,
                preview=preview,
            )
        except IntegrityError as exc:
            db.rollback()
            replay = PaymentAllocations._replay(
                db, key=key, fingerprint=payload.preview_fingerprint
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Allocation is already being recorded"
            ) from exc
        except Exception:
            db.rollback()
            raise

    @staticmethod
    def record_intent(
        db: Session,
        payload: PaymentAllocationCreate,
        *,
        commit: bool = True,
    ) -> PaymentAllocation:
        """Record invoice intent for pending payment facts without posting money."""
        payment = (
            db.query(Payment)
            .filter(Payment.id == payload.payment_id)
            .with_for_update()
            .first()
        )
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        if payment.status != PaymentStatus.pending or payment.settlement is not None:
            raise HTTPException(
                status_code=409,
                detail=("Settled payment allocation requires preview and confirmation"),
            )
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice or not invoice.is_active:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if payment.account_id is None or invoice.account_id != payment.account_id:
            raise HTTPException(
                status_code=400, detail="Invoice does not belong to payment account"
            )
        _validate_invoice_currency(invoice, payment.currency)
        _assert_invoice_allocatable(invoice)
        amount = round_money(to_decimal(payload.amount))
        if amount <= Decimal("0.00"):
            raise HTTPException(
                status_code=400, detail="Allocation amount must be positive"
            )
        existing = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.payment_id == payment.id)
            .filter(PaymentAllocation.invoice_id == invoice.id)
            .first()
        )
        if existing:
            if round_money(to_decimal(existing.amount)) != amount:
                raise HTTPException(
                    status_code=409,
                    detail="Payment already has different intent for this invoice",
                )
            return existing
        allocated = db.query(
            func.coalesce(func.sum(PaymentAllocation.amount), 0)
        ).filter(PaymentAllocation.payment_id == payment.id).filter(
            PaymentAllocation.is_active.is_(True)
        ).scalar() or Decimal("0.00")
        if round_money(to_decimal(allocated) + amount) > round_money(
            to_decimal(payment.amount)
        ):
            raise HTTPException(
                status_code=409, detail="Allocation intent exceeds payment amount"
            )
        allocation = PaymentAllocation(
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=amount,
            memo=payload.memo,
        )
        db.add(allocation)
        if commit:
            db.commit()
            db.refresh(allocation)
        else:
            db.flush()
        return allocation

    @staticmethod
    def create(db: Session, payload: PaymentAllocationCreate):
        return PaymentAllocations.record_intent(db, payload)

    @staticmethod
    def list(
        db: Session,
        payment_id: str | None,
        invoice_id: str | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentAllocation)
        if payment_id:
            query = query.filter(PaymentAllocation.payment_id == payment_id)
        if invoice_id:
            query = query.filter(PaymentAllocation.invoice_id == invoice_id)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentAllocation.created_at,
                "amount": PaymentAllocation.amount,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def delete(db: Session, allocation_id: str):
        allocation = get_by_id(db, PaymentAllocation, allocation_id)
        if not allocation:
            raise HTTPException(status_code=404, detail="Payment allocation not found")
        payment = get_by_id(db, Payment, allocation.payment_id)
        if payment and (payment.refunds or payment.reversal is not None):
            raise HTTPException(
                status_code=409,
                detail=("Evidence-backed refund or reversal allocations are immutable"),
            )
        if (
            allocation.ledger_entry_id is not None
            or allocation.consumption_ledger_entry_id is not None
            or (payment is not None and payment.settlement is not None)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Evidence-backed allocation cannot be deleted; use a reviewed "
                    "reversal workflow"
                ),
            )
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        # Pending intent has no money evidence, so it can be withdrawn without a
        # financial reversal. Evidence-backed allocations fail closed above.
        allocation.is_active = False
        if invoice:
            db.flush()
            _finalize_invoice_payment_effects(db, invoice)
        db.commit()


class CollectionAccounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: CollectionAccountCreate):
        account = CollectionAccount(**payload.model_dump())
        db.add(account)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def get(db: Session, account_id: str):
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        return account

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(CollectionAccount)
        if is_active is None:
            query = query.filter(CollectionAccount.is_active.is_(True))
        else:
            query = query.filter(CollectionAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": CollectionAccount.created_at,
                "name": CollectionAccount.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, account_id: str, payload: CollectionAccountUpdate):
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(account, key, value)
        db.commit()
        db.refresh(account)
        return account

    @staticmethod
    def delete(db: Session, account_id: str):
        account = get_by_id(db, CollectionAccount, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Collection account not found")
        account.is_active = False
        db.commit()


class PaymentChannels(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentChannelCreate):
        data = payload.model_dump()
        if data.get("default_collection_account_id"):
            _validate_collection_account(
                db, str(data["default_collection_account_id"]), None
            )
        if data.get("is_default"):
            db.query(PaymentChannel).filter(
                PaymentChannel.provider_id == data.get("provider_id"),
                PaymentChannel.is_default.is_(True),
            ).update({"is_default": False})
        channel = PaymentChannel(**data)
        db.add(channel)
        db.commit()
        db.refresh(channel)
        return channel

    @staticmethod
    def get(db: Session, channel_id: str):
        channel = get_by_id(db, PaymentChannel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        return channel

    @staticmethod
    def list(
        db: Session,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentChannel)
        if is_active is None:
            query = query.filter(PaymentChannel.is_active.is_(True))
        else:
            query = query.filter(PaymentChannel.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PaymentChannel.created_at, "name": PaymentChannel.name},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_sync(
        db: Session,
        *,
        is_active: bool | None,
        updated_since: datetime | None,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentChannel)
        if is_active is not None:
            query = query.filter(PaymentChannel.is_active == is_active)
        return apply_sync_page(
            query,
            PaymentChannel,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=kwargs["limit"], offset=kwargs["offset"])

    @staticmethod
    def update(db: Session, channel_id: str, payload: PaymentChannelUpdate):
        channel = get_by_id(db, PaymentChannel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        data = payload.model_dump(exclude_unset=True)
        if data.get("default_collection_account_id"):
            _validate_collection_account(
                db, str(data["default_collection_account_id"]), None
            )
        if data.get("is_default"):
            provider_id = data.get("provider_id", channel.provider_id)
            db.query(PaymentChannel).filter(
                PaymentChannel.provider_id == provider_id,
                PaymentChannel.id != channel.id,
                PaymentChannel.is_default.is_(True),
            ).update({"is_default": False})
        for key, value in data.items():
            setattr(channel, key, value)
        db.commit()
        db.refresh(channel)
        return channel

    @staticmethod
    def delete(db: Session, channel_id: str):
        channel = get_by_id(db, PaymentChannel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        channel.is_active = False
        db.commit()


class PaymentChannelAccounts(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentChannelAccountCreate):
        channel = _validate_payment_channel(db, str(payload.channel_id))
        if channel is None:
            raise HTTPException(status_code=404, detail="Payment channel not found")
        _validate_collection_account(
            db, str(payload.collection_account_id), payload.currency
        )
        if payload.is_default:
            db.query(PaymentChannelAccount).filter(
                PaymentChannelAccount.channel_id == channel.id,
                PaymentChannelAccount.currency == payload.currency,
                PaymentChannelAccount.is_default.is_(True),
            ).update({"is_default": False})
        mapping = PaymentChannelAccount(**payload.model_dump())
        db.add(mapping)
        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def get(db: Session, mapping_id: str):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(
                status_code=404, detail="Channel account mapping not found"
            )
        return mapping

    @staticmethod
    def list(
        db: Session,
        channel_id: str | None,
        collection_account_id: str | None,
        is_active: bool | None,
        order_by: str,
        order_dir: str,
        limit: int,
        offset: int,
    ):
        query = db.query(PaymentChannelAccount)
        if channel_id:
            query = query.filter(PaymentChannelAccount.channel_id == channel_id)
        if collection_account_id:
            query = query.filter(
                PaymentChannelAccount.collection_account_id == collection_account_id
            )
        if is_active is None:
            query = query.filter(PaymentChannelAccount.is_active.is_(True))
        else:
            query = query.filter(PaymentChannelAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": PaymentChannelAccount.created_at,
                "priority": PaymentChannelAccount.priority,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, mapping_id: str, payload: PaymentChannelAccountUpdate):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(
                status_code=404, detail="Channel account mapping not found"
            )
        data = payload.model_dump(exclude_unset=True)
        channel_id = data.get("channel_id", mapping.channel_id)
        currency = data.get("currency", mapping.currency)
        if "channel_id" in data:
            _validate_payment_channel(db, str(channel_id))
        if "collection_account_id" in data:
            _validate_collection_account(
                db, str(data["collection_account_id"]), currency
            )
        if data.get("is_default"):
            db.query(PaymentChannelAccount).filter(
                PaymentChannelAccount.channel_id == channel_id,
                PaymentChannelAccount.currency == currency,
                PaymentChannelAccount.id != mapping.id,
                PaymentChannelAccount.is_default.is_(True),
            ).update({"is_default": False})
        for key, value in data.items():
            setattr(mapping, key, value)
        db.commit()
        db.refresh(mapping)
        return mapping

    @staticmethod
    def delete(db: Session, mapping_id: str):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(
                status_code=404, detail="Channel account mapping not found"
            )
        mapping.is_active = False
        db.commit()


def _normalize_refund_key(value: str) -> str:
    key = value.strip()
    if not _REFUND_KEY_RE.fullmatch(key):
        raise HTTPException(status_code=400, detail="Invalid refund idempotency key")
    return key


def _refund_capability(
    payment: Payment, *, origin: PaymentRefundOrigin
) -> RefundCapability:
    if not payment.is_active:
        return RefundCapability(False, "Inactive payments cannot be refunded")
    if payment.account_id is None:
        return RefundCapability(
            False,
            "Consolidated billing-account refunds require their own owner workflow",
        )
    if payment.status == PaymentStatus.refunded:
        return RefundCapability(False, "Payment is already fully refunded")
    if payment.status not in {
        PaymentStatus.succeeded,
        PaymentStatus.partially_refunded,
    }:
        return RefundCapability(
            False,
            "Only succeeded or partially refunded payments can be refunded",
        )
    if origin == PaymentRefundOrigin.manual and payment.provider_id is not None:
        return RefundCapability(
            False,
            "Provider-backed payments require a confirmed provider refund event",
        )
    return RefundCapability(True, None)


def _validate_refund_provider_event(
    db: Session,
    *,
    payment: Payment,
    origin: PaymentRefundOrigin,
    provider_event_id: UUID | None,
) -> PaymentProviderEvent | None:
    if origin == PaymentRefundOrigin.manual:
        if provider_event_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Manual refunds cannot claim provider-event evidence",
            )
        return None
    if provider_event_id is None:
        raise HTTPException(
            status_code=409,
            detail="Provider refund evidence is required",
        )
    event = db.get(PaymentProviderEvent, provider_event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Payment provider event not found")
    if event.payment_id != payment.id:
        raise HTTPException(
            status_code=409,
            detail="Provider event does not belong to this payment",
        )
    if payment.provider_id is None or event.provider_id != payment.provider_id:
        raise HTTPException(
            status_code=409,
            detail="Provider event does not match the payment provider",
        )
    if event.financial_effect != PaymentProviderEventFinancialEffect.refund_confirmed:
        raise HTTPException(
            status_code=409,
            detail="Provider event is not confirmed refund evidence",
        )
    if event.amount is None:
        raise HTTPException(
            status_code=409,
            detail="Provider refund event has no normalized refund amount",
        )
    if event.currency != payment.currency:
        raise HTTPException(
            status_code=409,
            detail="Provider refund currency does not match the payment currency",
        )
    return event


def _recorded_refund_total(db: Session, payment: Payment) -> Decimal:
    ledger_total = round_money(
        to_decimal(
            db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
            .filter(LedgerEntry.payment_id == payment.id)
            .filter(LedgerEntry.source == LedgerSource.refund)
            .filter(LedgerEntry.is_active.is_(True))
            .scalar()
        )
    )
    document_total = round_money(to_decimal(payment.refunded_amount))
    if ledger_total != document_total:
        raise HTTPException(
            status_code=409,
            detail=(
                "Payment refund state conflicts with ledger evidence; "
                "reconcile it before another refund"
            ),
        )
    return ledger_total


def _refund_invoice_effects(
    db: Session,
    payment: Payment,
    *,
    gross: Decimal,
    refunded_before: Decimal,
    refunded_after: Decimal,
) -> tuple[tuple[PaymentRefundInvoiceEffect, ...], Decimal]:
    allocation_by_invoice: dict[UUID, Decimal] = {}
    for allocation in payment.allocations:
        if not allocation.is_active:
            continue
        allocation_by_invoice[allocation.invoice_id] = round_money(
            allocation_by_invoice.get(allocation.invoice_id, Decimal("0.00"))
            + to_decimal(allocation.amount)
        )

    effects: list[PaymentRefundInvoiceEffect] = []
    total_attributed = Decimal("0.00")
    for invoice_id, allocated in sorted(
        allocation_by_invoice.items(), key=lambda item: str(item[0])
    ):
        invoice = get_by_id(db, Invoice, invoice_id)
        if not invoice:
            raise HTTPException(
                status_code=409,
                detail="Payment allocation references a missing invoice",
            )
        contribution_before = round_money(allocated * (gross - refunded_before) / gross)
        contribution_after = round_money(allocated * (gross - refunded_after) / gross)
        attributed = round_money(contribution_before - contribution_after)
        receivable_before = round_money(to_decimal(invoice.balance_due))
        receivable_after = min(
            round_money(to_decimal(invoice.total)),
            round_money(receivable_before + attributed),
        )
        effects.append(
            PaymentRefundInvoiceEffect(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                receivable_before=receivable_before,
                receivable_after=receivable_after,
                refund_attributed=attributed,
            )
        )
        total_attributed = round_money(total_attributed + attributed)
    return tuple(effects), total_attributed


def _refund_fingerprint(
    *,
    payment: Payment,
    origin: PaymentRefundOrigin,
    provider_event_id: UUID | None,
    reason: str | None,
    gross: Decimal,
    refunded_before: Decimal,
    refund_amount: Decimal,
    funding_before: Decimal,
    account_credit_before: Decimal,
    account_credit_consumption: Decimal,
    invoice_effects: tuple[PaymentRefundInvoiceEffect, ...],
) -> str:
    payload = {
        "kind": "payment_refund",
        "payment_id": str(payment.id),
        "account_id": str(payment.account_id),
        "origin": origin.value,
        "provider_event_id": str(provider_event_id) if provider_event_id else None,
        "reason": reason,
        "currency": payment.currency,
        "gross": f"{gross:.2f}",
        "refunded_before": f"{refunded_before:.2f}",
        "refund_amount": f"{refund_amount:.2f}",
        "funding_before": f"{funding_before:.2f}",
        "account_credit_before": f"{account_credit_before:.2f}",
        "account_credit_consumption": f"{account_credit_consumption:.2f}",
        "invoice_effects": [
            {
                "invoice_id": str(effect.invoice_id),
                "receivable_before": f"{effect.receivable_before:.2f}",
                "receivable_after": f"{effect.receivable_after:.2f}",
                "refund_attributed": f"{effect.refund_attributed:.2f}",
            }
            for effect in invoice_effects
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_refund_preview(
    db: Session,
    payment: Payment,
    payload: PaymentRefundPreviewRequest,
    *,
    origin: PaymentRefundOrigin,
    provider_event_id: UUID | None = None,
) -> PaymentRefundPreview:
    capability = _refund_capability(payment, origin=origin)
    if not capability.allowed:
        raise HTTPException(status_code=409, detail=capability.reason)
    provider_event = _validate_refund_provider_event(
        db,
        payment=payment,
        origin=origin,
        provider_event_id=provider_event_id,
    )
    assert payment.account_id is not None
    gross = round_money(to_decimal(payment.amount))
    if gross <= 0:
        raise HTTPException(
            status_code=409, detail="A non-positive payment cannot be refunded"
        )
    refunded_before = _recorded_refund_total(db, payment)
    refundable = round_money(gross - refunded_before)
    amount = (
        refundable
        if payload.amount is None
        else round_money(to_decimal(payload.amount))
    )
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Refund amount must be positive")
    if amount > refundable:
        raise HTTPException(
            status_code=400,
            detail=f"Refund amount exceeds refundable balance ({refundable})",
        )
    if provider_event is not None and amount != round_money(
        to_decimal(provider_event.amount)
    ):
        raise HTTPException(
            status_code=409,
            detail="Refund amount does not match the normalized provider event",
        )
    refunded_after = round_money(refunded_before + amount)
    status_after = (
        PaymentStatus.refunded
        if refunded_after == gross
        else PaymentStatus.partially_refunded
    )
    invoice_effects, invoice_attributed = _refund_invoice_effects(
        db,
        payment,
        gross=gross,
        refunded_before=refunded_before,
        refunded_after=refunded_after,
    )
    account_credit_consumption = round_money(amount - invoice_attributed)
    if account_credit_consumption < 0:
        raise HTTPException(
            status_code=409,
            detail="Payment allocations exceed the refundable payment amount",
        )
    funding_before = calculate_customer_balance(
        db, payment.account_id, currency=payment.currency
    )
    account_credit_before = get_account_credit_balance(
        db, str(payment.account_id), currency=payment.currency
    )
    fingerprint = _refund_fingerprint(
        payment=payment,
        origin=origin,
        provider_event_id=provider_event_id,
        reason=payload.reason,
        gross=gross,
        refunded_before=refunded_before,
        refund_amount=amount,
        funding_before=funding_before,
        account_credit_before=account_credit_before,
        account_credit_consumption=account_credit_consumption,
        invoice_effects=invoice_effects,
    )
    return PaymentRefundPreview(
        payment_id=payment.id,
        account_id=payment.account_id,
        currency=payment.currency,
        payment_gross=gross,
        refunded_before=refunded_before,
        refundable_before=refundable,
        refund_amount=amount,
        refunded_after=refunded_after,
        payment_net_after=round_money(gross - refunded_after),
        status_after=status_after,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=round_money(funding_before - amount),
        account_credit_before=account_credit_before,
        account_credit_after=round_money(
            account_credit_before - account_credit_consumption
        ),
        account_credit_consumption=account_credit_consumption,
        invoice_effects=invoice_effects,
        ledger_entry_type=LedgerEntryType.debit,
        ledger_source=LedgerSource.refund,
        ledger_amount=amount,
        access_consequence="recheck_after_refund",
        fingerprint=fingerprint,
    )


def _stage_refund_audit(
    db: Session,
    *,
    refund: PaymentRefund,
    preview: PaymentRefundPreview,
    ledger_entry: LedgerEntry,
    consumption_entry: LedgerEntry | None,
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="refund",
            entity_type="payment",
            entity_id=str(refund.payment_id),
            metadata_={
                "refund_id": str(refund.id),
                "ledger_entry_id": str(ledger_entry.id),
                "credit_consumption_ledger_entry_id": (
                    str(consumption_entry.id) if consumption_entry else None
                ),
                "amount": str(refund.amount),
                "currency": refund.currency,
                "origin": refund.origin.value,
                "provider_event_id": (
                    str(refund.provider_event_id) if refund.provider_event_id else None
                ),
                "preview_fingerprint": preview.fingerprint,
                "prepaid_funding_before": str(preview.prepaid_funding_before),
                "prepaid_funding_after": str(preview.prepaid_funding_after),
                "account_credit_before": str(preview.account_credit_before),
                "account_credit_after": str(preview.account_credit_after),
                "access_consequence": preview.access_consequence,
            },
        ),
    )


class Refunds:
    """Canonical completed-refund projection owner."""

    @staticmethod
    def capability(db: Session, payment_id: str) -> RefundCapability:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _refund_capability(payment, origin=PaymentRefundOrigin.manual)

    @staticmethod
    def preview(
        db: Session,
        payment_id: str,
        payload: PaymentRefundPreviewRequest,
    ) -> PaymentRefundPreview:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _build_refund_preview(
            db, payment, payload, origin=PaymentRefundOrigin.manual
        )

    @staticmethod
    def _idempotent_result(
        db: Session,
        *,
        key: str,
        payment_id: str,
        preview_fingerprint: str,
    ) -> PaymentRefundResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _REFUND_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(status_code=409, detail="Refund is being processed")
        refund = get_by_id(db, PaymentRefund, reservation.ref_id)
        if not refund:
            raise HTTPException(status_code=409, detail="Refund evidence is incomplete")
        if str(refund.payment_id) != str(payment_id):
            raise HTTPException(
                status_code=409, detail="Idempotency key belongs to another payment"
            )
        if refund.preview_fingerprint != preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was used with a different refund confirmation",
            )
        payment = get_by_id(db, Payment, refund.payment_id)
        ledger_entry = db.get(LedgerEntry, refund.ledger_entry_id)
        if not payment or not ledger_entry:
            raise HTTPException(
                status_code=409, detail="Refund ledger evidence was not found"
            )
        consumption = (
            db.get(LedgerEntry, refund.credit_consumption_ledger_entry_id)
            if refund.credit_consumption_ledger_entry_id
            else None
        )
        if refund.credit_consumption_ledger_entry_id and not consumption:
            raise HTTPException(
                status_code=409, detail="Refund account-credit evidence was not found"
            )
        return PaymentRefundResult(
            refund=refund,
            payment=payment,
            ledger_entry=ledger_entry,
            credit_consumption_ledger_entry=consumption,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def process_with_evidence(
        db: Session,
        payment_id: str,
        payload: PaymentRefundRequest,
        *,
        origin: PaymentRefundOrigin = PaymentRefundOrigin.manual,
        provider_event_id: UUID | None = None,
        commit: bool = True,
        stage_audit: bool = True,
    ) -> PaymentRefundResult:
        key = _normalize_refund_key(payload.idempotency_key)
        replay = Refunds._idempotent_result(
            db,
            key=key,
            payment_id=payment_id,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay:
            return replay
        initial = get_by_id(db, Payment, payment_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated billing-account refunds require their own owner workflow",
            )
        lock_account(db, str(initial.account_id))
        payment = lock_for_update(db, Payment, initial.id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        assert payment.account_id is not None
        preview = _build_refund_preview(
            db,
            payment,
            PaymentRefundPreviewRequest(
                amount=payload.amount,
                reason=payload.reason,
            ),
            origin=origin,
            provider_event_id=provider_event_id,
        )
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = Refunds._idempotent_result(
            db,
            key=key,
            payment_id=payment_id,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay:
            return replay
        reservation = IdempotencyKey(
            scope=_REFUND_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=payment.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
            ledger_entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=payment.account_id,
                    payment_id=payment.id,
                    entry_type=preview.ledger_entry_type,
                    source=preview.ledger_source,
                    amount=preview.ledger_amount,
                    currency=preview.currency,
                    memo=payload.reason or f"Refund of payment {payment.id}",
                ),
                commit=False,
            )
            consumption_entry = None
            if preview.account_credit_consumption > 0:
                consumption_entry = LedgerEntries.create(
                    db,
                    LedgerEntryCreate(
                        account_id=payment.account_id,
                        entry_type=LedgerEntryType.debit,
                        source=LedgerSource.other,
                        amount=preview.account_credit_consumption,
                        currency=preview.currency,
                        memo=(
                            f"{_REFUND_CONSUMPTION_MEMO_PREFIX} payment {payment.id}"
                        ),
                    ),
                    commit=False,
                )
            refund = PaymentRefund(
                payment_id=payment.id,
                provider_event_id=provider_event_id,
                ledger_entry_id=ledger_entry.id,
                credit_consumption_ledger_entry_id=(
                    consumption_entry.id if consumption_entry else None
                ),
                amount=preview.refund_amount,
                currency=preview.currency,
                origin=origin,
                reason=payload.reason,
                preview_fingerprint=preview.fingerprint,
            )
            db.add(refund)
            db.flush()
            reservation.ref_id = str(refund.id)
            payment.status = preview.status_after
            payment.refunded_amount = preview.refunded_after

            invoice_ids = {
                allocation.invoice_id
                for allocation in payment.allocations
                if allocation.is_active
            }
            if preview.status_after == PaymentStatus.refunded:
                for allocation in payment.allocations:
                    if not allocation.is_active:
                        continue
                    db.query(LedgerEntry).filter(
                        LedgerEntry.payment_id == allocation.payment_id,
                        LedgerEntry.invoice_id == allocation.invoice_id,
                        LedgerEntry.source == LedgerSource.payment,
                    ).update({"is_active": False}, synchronize_session=False)
                    allocation.is_active = False
            db.flush()
            for invoice_id in invoice_ids:
                invoice = get_by_id(db, Invoice, invoice_id)
                if invoice:
                    _finalize_invoice_payment_effects(db, invoice)

            from app.services.account_lifecycle import compute_account_status

            compute_account_status(db, str(payment.account_id))
            if stage_audit:
                _stage_refund_audit(
                    db,
                    refund=refund,
                    preview=preview,
                    ledger_entry=ledger_entry,
                    consumption_entry=consumption_entry,
                )
            emit_event(
                db,
                EventType.payment_refunded,
                {
                    "payment_id": str(payment.id),
                    "refund_id": str(refund.id),
                    "amount": str(preview.refund_amount),
                    "refund_amount": str(preview.refund_amount),
                    "currency": payment.currency,
                    "reason": payload.reason,
                    "is_full_refund": (preview.status_after == PaymentStatus.refunded),
                    "ledger_entry_id": str(ledger_entry.id),
                },
                account_id=payment.account_id,
            )
            db.flush()
            if commit:
                db.commit()
                db.refresh(payment)
                db.refresh(refund)
                db.refresh(ledger_entry)
                if consumption_entry:
                    db.refresh(consumption_entry)
        except IntegrityError as exc:
            db.rollback()
            replay = Refunds._idempotent_result(
                db,
                key=key,
                payment_id=payment_id,
                preview_fingerprint=payload.preview_fingerprint,
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Refund is already being processed"
            ) from exc
        except Exception:
            db.rollback()
            raise
        return PaymentRefundResult(
            refund=refund,
            payment=payment,
            ledger_entry=ledger_entry,
            credit_consumption_ledger_entry=consumption_entry,
            preview=preview,
        )

    @staticmethod
    def process_provider_event_refund(
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        commit: bool = False,
    ) -> PaymentRefundResult:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        event = _validate_refund_provider_event(
            db,
            payment=payment,
            origin=PaymentRefundOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        assert event is not None
        request = PaymentRefundPreviewRequest(
            amount=event.amount,
            reason=f"Confirmed provider refund event {provider_event_id}",
        )
        preview = _build_refund_preview(
            db,
            payment,
            request,
            origin=PaymentRefundOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        return Refunds.process_with_evidence(
            db,
            payment_id,
            PaymentRefundRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=f"provider-refund-{provider_event_id}",
            ),
            origin=PaymentRefundOrigin.provider_event,
            provider_event_id=provider_event_id,
            commit=commit,
        )

    @staticmethod
    def process_refund(
        db: Session,
        payment_id: str,
        refund_amount: Decimal | None = None,
        reason: str | None = None,
        create_credit_note: bool = False,
        *,
        idempotency_key: str | None = None,
    ) -> Payment:
        """Compatibility command using the same owner preview and confirmation."""
        if create_credit_note:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A refund and a credit note are separate financial actions; "
                    "do not grant both for one amount"
                ),
            )
        if not idempotency_key:
            raise HTTPException(
                status_code=400,
                detail="Refund idempotency key is required",
            )
        if refund_amount is not None and to_decimal(refund_amount) <= 0:
            raise HTTPException(
                status_code=400, detail="Refund amount must be positive"
            )
        request = PaymentRefundPreviewRequest(amount=refund_amount, reason=reason)
        preview = Refunds.preview(db, payment_id, request)
        return Refunds.process_with_evidence(
            db,
            payment_id,
            PaymentRefundRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
            ),
        ).payment

    @staticmethod
    def inspect_evidence(
        db: Session, payment_id: str
    ) -> PaymentRefundEvidenceInspection:
        """Report historical refund rows that lack structural evidence links."""
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        recorded_total = _recorded_refund_total(db, payment)
        rows = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.payment_id == payment.id)
            .filter(LedgerEntry.source == LedgerSource.refund)
            .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(LedgerEntry.is_active.is_(True))
            .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
            .all()
        )
        linked = {
            row[0]
            for row in db.query(PaymentRefund.ledger_entry_id)
            .filter(PaymentRefund.payment_id == payment.id)
            .all()
        }
        return PaymentRefundEvidenceInspection(
            payment_id=payment.id,
            recorded_refund_total=recorded_total,
            linked_ledger_entry_ids=tuple(row.id for row in rows if row.id in linked),
            unlinked_ledger_entry_ids=tuple(
                row.id for row in rows if row.id not in linked
            ),
        )

    @staticmethod
    def reconcile_evidence(
        db: Session,
        payment_id: str,
        *,
        refund_ledger_entry_id: UUID,
        account_credit_consumption: Decimal = Decimal("0.00"),
        provider_event_id: UUID | None = None,
        commit: bool = True,
    ) -> PaymentRefund:
        """Link one explicitly selected historical refund row; never infer one.

        If the historical refund consumed unallocated account credit, the
        reviewed amount must also be supplied explicitly. The reconciler posts
        and links that exact internal consumption row instead of deriving it
        from today's UI or balance.
        """
        initial = get_by_id(db, Payment, payment_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payment refund evidence needs its own owner",
            )
        lock_account(db, str(initial.account_id))
        payment = lock_for_update(db, Payment, initial.id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        _recorded_refund_total(db, payment)
        requested_consumption = round_money(to_decimal(account_credit_consumption))
        existing = db.scalar(
            select(PaymentRefund).where(
                PaymentRefund.ledger_entry_id == refund_ledger_entry_id
            )
        )
        if existing:
            if existing.payment_id != payment.id:
                raise HTTPException(
                    status_code=409,
                    detail="Refund ledger evidence belongs to another payment",
                )
            existing_consumption = Decimal("0.00")
            if existing.credit_consumption_ledger_entry_id:
                existing_entry = db.get(
                    LedgerEntry, existing.credit_consumption_ledger_entry_id
                )
                if not existing_entry:
                    raise HTTPException(
                        status_code=409,
                        detail="Linked account-credit evidence is missing",
                    )
                existing_consumption = round_money(to_decimal(existing_entry.amount))
            if (
                existing_consumption != requested_consumption
                or existing.provider_event_id != provider_event_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Refund evidence was already reconciled differently",
                )
            return existing
        entry = lock_for_update(db, LedgerEntry, refund_ledger_entry_id)
        if not entry:
            raise HTTPException(status_code=404, detail="Refund ledger entry not found")
        if (
            entry.payment_id != payment.id
            or entry.account_id != payment.account_id
            or entry.source != LedgerSource.refund
            or entry.entry_type != LedgerEntryType.debit
            or entry.currency != payment.currency
            or not entry.is_active
        ):
            raise HTTPException(
                status_code=409,
                detail="Selected ledger entry is not exact active refund evidence",
            )
        origin = PaymentRefundOrigin.manual
        if payment.provider_id is not None:
            event = _validate_refund_provider_event(
                db,
                payment=payment,
                origin=PaymentRefundOrigin.provider_event,
                provider_event_id=provider_event_id,
            )
            assert event is not None
            if round_money(to_decimal(event.amount)) != round_money(
                to_decimal(entry.amount)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Provider event amount does not match refund ledger evidence",
                )
            origin = PaymentRefundOrigin.provider_event
        elif provider_event_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Manual payment evidence cannot claim a provider event",
            )
        consumption = requested_consumption
        if consumption < 0 or consumption > round_money(to_decimal(entry.amount)):
            raise HTTPException(
                status_code=400,
                detail="Reviewed account-credit consumption is outside the refund amount",
            )
        consumption_entry = None
        if consumption > 0:
            consumption_entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=payment.account_id,
                    entry_type=LedgerEntryType.debit,
                    source=LedgerSource.other,
                    amount=consumption,
                    currency=payment.currency,
                    memo=(
                        f"{_REFUND_CONSUMPTION_MEMO_PREFIX} historical refund "
                        f"ledger {entry.id}"
                    ),
                ),
                commit=False,
            )
        refund = PaymentRefund(
            payment_id=payment.id,
            provider_event_id=provider_event_id,
            ledger_entry_id=entry.id,
            credit_consumption_ledger_entry_id=(
                consumption_entry.id if consumption_entry else None
            ),
            amount=entry.amount,
            currency=entry.currency,
            origin=origin,
            reason="Historical refund evidence reconciliation",
            preview_fingerprint=None,
        )
        db.add(refund)
        db.flush()
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action="reconcile_refund_evidence",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata_={
                    "refund_id": str(refund.id),
                    "ledger_entry_id": str(entry.id),
                    "credit_consumption_ledger_entry_id": (
                        str(consumption_entry.id) if consumption_entry else None
                    ),
                    "reviewed_account_credit_consumption": str(consumption),
                    "provider_event_id": (
                        str(provider_event_id) if provider_event_id else None
                    ),
                },
            ),
        )
        if commit:
            db.commit()
            db.refresh(refund)
        return refund

    @staticmethod
    def reverse_payment(
        db: Session,
        payment_id: str,
        reason: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Payment:
        """Compatibility command routed through the reversal owner."""
        if not idempotency_key:
            raise HTTPException(
                status_code=400, detail="Payment reversal idempotency key is required"
            )
        if not reason or len(reason.strip()) < 3:
            raise HTTPException(
                status_code=400, detail="Payment reversal reason is required"
            )
        request = PaymentReversalPreviewRequest(reason=reason.strip())
        preview = PaymentReversals.preview(db, payment_id, request)
        return PaymentReversals.process_with_evidence(
            db,
            payment_id,
            PaymentReversalRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=idempotency_key,
            ),
        ).payment


def _normalize_reversal_key(value: str) -> str:
    key = value.strip()
    if not _REFUND_KEY_RE.fullmatch(key):
        raise HTTPException(
            status_code=400,
            detail=("Payment reversal idempotency key must be 16-120 safe characters"),
        )
    return key


def _reversal_capability(
    payment: Payment, *, origin: PaymentReversalOrigin
) -> ReversalCapability:
    if not payment.is_active:
        return ReversalCapability(False, "Inactive payment cannot be reversed")
    if payment.account_id is None:
        return ReversalCapability(
            False,
            "Consolidated billing-account reversals require their own owner workflow",
        )
    if payment.reversal is not None or payment.status == PaymentStatus.reversed:
        return ReversalCapability(False, "Payment is already reversed")
    if payment.status not in {
        PaymentStatus.succeeded,
        PaymentStatus.partially_refunded,
    }:
        return ReversalCapability(
            False,
            "Only settled payment value can be reversed",
        )
    if origin == PaymentReversalOrigin.manual and payment.provider_id is not None:
        return ReversalCapability(
            False,
            "Provider-backed payments require a confirmed provider reversal event",
        )
    return ReversalCapability(True, None)


def _validate_reversal_provider_event(
    db: Session,
    *,
    payment: Payment,
    origin: PaymentReversalOrigin,
    provider_event_id: UUID | None,
) -> PaymentProviderEvent | None:
    if origin == PaymentReversalOrigin.manual:
        if provider_event_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Manual reversals cannot claim provider-event evidence",
            )
        return None
    if provider_event_id is None:
        raise HTTPException(
            status_code=409, detail="Provider reversal evidence is required"
        )
    event = db.get(PaymentProviderEvent, provider_event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Payment provider event not found")
    if event.payment_id != payment.id:
        raise HTTPException(
            status_code=409,
            detail="Provider event does not belong to this payment",
        )
    if payment.provider_id is None or event.provider_id != payment.provider_id:
        raise HTTPException(
            status_code=409,
            detail="Provider event does not match the payment provider",
        )
    if event.financial_effect != PaymentProviderEventFinancialEffect.reversal_confirmed:
        raise HTTPException(
            status_code=409,
            detail="Provider event is not confirmed reversal evidence",
        )
    if event.amount is None:
        raise HTTPException(
            status_code=409,
            detail="Provider reversal event has no normalized reversal amount",
        )
    if event.currency != payment.currency:
        raise HTTPException(
            status_code=409,
            detail="Provider reversal currency does not match the payment currency",
        )
    return event


def _reversal_fingerprint(
    *,
    payment: Payment,
    origin: PaymentReversalOrigin,
    provider_event_id: UUID | None,
    reason: str,
    gross: Decimal,
    refunded_before: Decimal,
    net_before: Decimal,
    funding_before: Decimal,
    account_credit_before: Decimal,
    account_credit_consumption: Decimal,
    invoice_effects: tuple[PaymentRefundInvoiceEffect, ...],
) -> str:
    payload = {
        "kind": "payment_reversal",
        "payment_id": str(payment.id),
        "account_id": str(payment.account_id),
        "origin": origin.value,
        "provider_event_id": str(provider_event_id) if provider_event_id else None,
        "reason": reason,
        "currency": payment.currency,
        "gross": f"{gross:.2f}",
        "refunded_before": f"{refunded_before:.2f}",
        "net_before": f"{net_before:.2f}",
        "funding_before": f"{funding_before:.2f}",
        "account_credit_before": f"{account_credit_before:.2f}",
        "account_credit_consumption": f"{account_credit_consumption:.2f}",
        "invoice_effects": [
            {
                "invoice_id": str(effect.invoice_id),
                "receivable_before": f"{effect.receivable_before:.2f}",
                "receivable_after": f"{effect.receivable_after:.2f}",
                "reversal_attributed": f"{effect.refund_attributed:.2f}",
            }
            for effect in invoice_effects
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_reversal_preview(
    db: Session,
    payment: Payment,
    payload: PaymentReversalPreviewRequest,
    *,
    origin: PaymentReversalOrigin,
    provider_event_id: UUID | None = None,
) -> PaymentReversalPreview:
    capability = _reversal_capability(payment, origin=origin)
    if not capability.allowed:
        raise HTTPException(status_code=409, detail=capability.reason)
    provider_event = _validate_reversal_provider_event(
        db,
        payment=payment,
        origin=origin,
        provider_event_id=provider_event_id,
    )
    assert payment.account_id is not None
    reason = payload.reason.strip()
    gross = round_money(to_decimal(payment.amount))
    refunded_before = _recorded_refund_total(db, payment)
    net_before = round_money(gross - refunded_before)
    if net_before <= 0:
        raise HTTPException(
            status_code=409, detail="Payment has no settled value left to reverse"
        )
    if (
        provider_event is not None
        and round_money(to_decimal(provider_event.amount)) != net_before
    ):
        raise HTTPException(
            status_code=409,
            detail="Provider reversal amount does not match remaining payment value",
        )
    invoice_effects, invoice_attributed = _refund_invoice_effects(
        db,
        payment,
        gross=gross,
        refunded_before=refunded_before,
        refunded_after=gross,
    )
    account_credit_consumption = round_money(net_before - invoice_attributed)
    if account_credit_consumption < 0:
        raise HTTPException(
            status_code=409,
            detail="Payment allocations exceed remaining payment value",
        )
    funding_before = calculate_customer_balance(
        db, payment.account_id, currency=payment.currency
    )
    account_credit_before = get_account_credit_balance(
        db, str(payment.account_id), currency=payment.currency
    )
    return PaymentReversalPreview(
        payment_id=payment.id,
        account_id=payment.account_id,
        currency=payment.currency,
        payment_gross=gross,
        refunded_before=refunded_before,
        payment_net_before=net_before,
        reversal_amount=net_before,
        status_after=PaymentStatus.reversed,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=round_money(funding_before - net_before),
        account_credit_before=account_credit_before,
        account_credit_after=round_money(
            account_credit_before - account_credit_consumption
        ),
        account_credit_consumption=account_credit_consumption,
        invoice_effects=invoice_effects,
        ledger_entry_type=LedgerEntryType.debit,
        ledger_source=LedgerSource.payment,
        ledger_amount=net_before,
        access_consequence="recheck_after_payment_reversal",
        fingerprint=_reversal_fingerprint(
            payment=payment,
            origin=origin,
            provider_event_id=provider_event_id,
            reason=reason,
            gross=gross,
            refunded_before=refunded_before,
            net_before=net_before,
            funding_before=funding_before,
            account_credit_before=account_credit_before,
            account_credit_consumption=account_credit_consumption,
            invoice_effects=invoice_effects,
        ),
    )


def _stage_reversal_audit(
    db: Session,
    *,
    reversal: PaymentReversal,
    preview: PaymentReversalPreview,
    ledger_entry: LedgerEntry,
    consumption_entry: LedgerEntry | None,
) -> None:
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.system,
            action="reverse",
            entity_type="payment",
            entity_id=str(reversal.payment_id),
            metadata_={
                "reversal_id": str(reversal.id),
                "ledger_entry_id": str(ledger_entry.id),
                "credit_consumption_ledger_entry_id": (
                    str(consumption_entry.id) if consumption_entry else None
                ),
                "amount": str(reversal.amount),
                "currency": reversal.currency,
                "origin": reversal.origin.value,
                "provider_event_id": (
                    str(reversal.provider_event_id)
                    if reversal.provider_event_id
                    else None
                ),
                "preview_fingerprint": preview.fingerprint,
                "prepaid_funding_before": str(preview.prepaid_funding_before),
                "prepaid_funding_after": str(preview.prepaid_funding_after),
                "account_credit_before": str(preview.account_credit_before),
                "account_credit_after": str(preview.account_credit_after),
                "access_consequence": preview.access_consequence,
            },
        ),
    )


class PaymentReversals:
    """Canonical chargeback and bank-reversal projection owner."""

    @staticmethod
    def capability(db: Session, payment_id: str) -> ReversalCapability:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _reversal_capability(payment, origin=PaymentReversalOrigin.manual)

    @staticmethod
    def preview(
        db: Session,
        payment_id: str,
        payload: PaymentReversalPreviewRequest,
    ) -> PaymentReversalPreview:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        return _build_reversal_preview(
            db, payment, payload, origin=PaymentReversalOrigin.manual
        )

    @staticmethod
    def _idempotent_result(
        db: Session,
        *,
        key: str,
        payment_id: str,
        preview_fingerprint: str,
    ) -> PaymentReversalResult | None:
        reservation = db.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _REVERSAL_IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == key,
            )
        )
        if reservation is None:
            return None
        if not reservation.ref_id:
            raise HTTPException(
                status_code=409, detail="Payment reversal is being processed"
            )
        reversal = get_by_id(db, PaymentReversal, reservation.ref_id)
        if not reversal:
            raise HTTPException(
                status_code=409, detail="Payment reversal evidence is incomplete"
            )
        if str(reversal.payment_id) != str(payment_id):
            raise HTTPException(
                status_code=409,
                detail="Idempotency key belongs to another payment",
            )
        if reversal.preview_fingerprint != preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency key was used with a different reversal confirmation"
                ),
            )
        payment = get_by_id(db, Payment, reversal.payment_id)
        ledger_entry = db.get(LedgerEntry, reversal.ledger_entry_id)
        if not payment or not ledger_entry:
            raise HTTPException(
                status_code=409, detail="Payment reversal ledger evidence was not found"
            )
        consumption = (
            db.get(LedgerEntry, reversal.credit_consumption_ledger_entry_id)
            if reversal.credit_consumption_ledger_entry_id
            else None
        )
        if reversal.credit_consumption_ledger_entry_id and not consumption:
            raise HTTPException(
                status_code=409,
                detail="Payment reversal account-credit evidence was not found",
            )
        return PaymentReversalResult(
            reversal=reversal,
            payment=payment,
            ledger_entry=ledger_entry,
            credit_consumption_ledger_entry=consumption,
            preview=None,
            idempotent_replay=True,
        )

    @staticmethod
    def process_with_evidence(
        db: Session,
        payment_id: str,
        payload: PaymentReversalRequest,
        *,
        origin: PaymentReversalOrigin = PaymentReversalOrigin.manual,
        provider_event_id: UUID | None = None,
        commit: bool = True,
        stage_audit: bool = True,
    ) -> PaymentReversalResult:
        key = _normalize_reversal_key(payload.idempotency_key)
        replay = PaymentReversals._idempotent_result(
            db,
            key=key,
            payment_id=payment_id,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay:
            return replay
        initial = get_by_id(db, Payment, payment_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.account_id is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Consolidated billing-account reversals require their own "
                    "owner workflow"
                ),
            )
        lock_account(db, str(initial.account_id))
        payment = lock_for_update(db, Payment, initial.id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        assert payment.account_id is not None
        preview = _build_reversal_preview(
            db,
            payment,
            PaymentReversalPreviewRequest(reason=payload.reason),
            origin=origin,
            provider_event_id=provider_event_id,
        )
        if preview.fingerprint != payload.preview_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Financial state changed after preview; preview again",
            )
        replay = PaymentReversals._idempotent_result(
            db,
            key=key,
            payment_id=payment_id,
            preview_fingerprint=payload.preview_fingerprint,
        )
        if replay:
            return replay
        reservation = IdempotencyKey(
            scope=_REVERSAL_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=payment.account_id,
        )
        db.add(reservation)
        try:
            db.flush()
            ledger_entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=payment.account_id,
                    payment_id=payment.id,
                    entry_type=preview.ledger_entry_type,
                    source=preview.ledger_source,
                    amount=preview.ledger_amount,
                    currency=preview.currency,
                    memo=(f"Payment reversal: {payload.reason.strip()}"),
                ),
                commit=False,
            )
            consumption_entry = None
            if preview.account_credit_consumption > 0:
                consumption_entry = LedgerEntries.create(
                    db,
                    LedgerEntryCreate(
                        account_id=payment.account_id,
                        entry_type=LedgerEntryType.debit,
                        source=LedgerSource.other,
                        amount=preview.account_credit_consumption,
                        currency=preview.currency,
                        memo=(
                            f"{_REVERSAL_CONSUMPTION_MEMO_PREFIX} payment {payment.id}"
                        ),
                    ),
                    commit=False,
                )
            reversal = PaymentReversal(
                payment_id=payment.id,
                provider_event_id=provider_event_id,
                ledger_entry_id=ledger_entry.id,
                credit_consumption_ledger_entry_id=(
                    consumption_entry.id if consumption_entry else None
                ),
                amount=preview.reversal_amount,
                currency=preview.currency,
                origin=origin,
                reason=payload.reason.strip(),
                preview_fingerprint=preview.fingerprint,
            )
            db.add(reversal)
            db.flush()
            reservation.ref_id = str(reversal.id)
            payment.status = PaymentStatus.reversed
            invoice_ids = {
                allocation.invoice_id
                for allocation in payment.allocations
                if allocation.is_active
            }
            db.flush()
            for invoice_id in invoice_ids:
                invoice = get_by_id(db, Invoice, invoice_id)
                if invoice:
                    _finalize_invoice_payment_effects(db, invoice)

            from app.services.account_lifecycle import compute_account_status

            compute_account_status(db, str(payment.account_id))
            if stage_audit:
                _stage_reversal_audit(
                    db,
                    reversal=reversal,
                    preview=preview,
                    ledger_entry=ledger_entry,
                    consumption_entry=consumption_entry,
                )
            emit_event(
                db,
                EventType.payment_reversed,
                {
                    "payment_id": str(payment.id),
                    "reversal_id": str(reversal.id),
                    "amount": str(preview.reversal_amount),
                    "currency": payment.currency,
                    "reason": payload.reason.strip(),
                    "from_status": (
                        PaymentStatus.partially_refunded.value
                        if preview.refunded_before > 0
                        else PaymentStatus.succeeded.value
                    ),
                    "to_status": PaymentStatus.reversed.value,
                    "ledger_entry_id": str(ledger_entry.id),
                },
                account_id=payment.account_id,
            )
            db.flush()
            if commit:
                db.commit()
                db.refresh(payment)
                db.refresh(reversal)
                db.refresh(ledger_entry)
                if consumption_entry:
                    db.refresh(consumption_entry)
        except IntegrityError as exc:
            db.rollback()
            replay = PaymentReversals._idempotent_result(
                db,
                key=key,
                payment_id=payment_id,
                preview_fingerprint=payload.preview_fingerprint,
            )
            if replay:
                return replay
            raise HTTPException(
                status_code=409, detail="Payment reversal is already being processed"
            ) from exc
        except Exception:
            db.rollback()
            raise
        return PaymentReversalResult(
            reversal=reversal,
            payment=payment,
            ledger_entry=ledger_entry,
            credit_consumption_ledger_entry=consumption_entry,
            preview=preview,
        )

    @staticmethod
    def process_provider_event_reversal(
        db: Session,
        *,
        payment_id: str,
        provider_event_id: UUID,
        commit: bool = False,
    ) -> PaymentReversalResult:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        event = _validate_reversal_provider_event(
            db,
            payment=payment,
            origin=PaymentReversalOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        assert event is not None
        request = PaymentReversalPreviewRequest(
            reason=f"Confirmed provider reversal event {provider_event_id}"
        )
        preview = _build_reversal_preview(
            db,
            payment,
            request,
            origin=PaymentReversalOrigin.provider_event,
            provider_event_id=provider_event_id,
        )
        return PaymentReversals.process_with_evidence(
            db,
            payment_id,
            PaymentReversalRequest(
                **request.model_dump(),
                preview_fingerprint=preview.fingerprint,
                idempotency_key=f"provider-reversal-{provider_event_id}",
            ),
            origin=PaymentReversalOrigin.provider_event,
            provider_event_id=provider_event_id,
            commit=commit,
        )

    @staticmethod
    def inspect_evidence(
        db: Session, payment_id: str
    ) -> PaymentReversalEvidenceInspection:
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        net = round_money(
            to_decimal(payment.amount) - to_decimal(payment.refunded_amount)
        )
        rows = (
            db.query(LedgerEntry)
            .filter(LedgerEntry.payment_id == payment.id)
            .filter(LedgerEntry.entry_type == LedgerEntryType.debit)
            .filter(LedgerEntry.source.in_([LedgerSource.payment, LedgerSource.refund]))
            .filter(LedgerEntry.is_active.is_(True))
            .order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc())
            .all()
        )
        refund_ids = {
            row[0]
            for row in db.query(PaymentRefund.ledger_entry_id)
            .filter(PaymentRefund.payment_id == payment.id)
            .all()
        }
        linked = {
            row[0]
            for row in db.query(PaymentReversal.ledger_entry_id)
            .filter(PaymentReversal.payment_id == payment.id)
            .all()
        }
        return PaymentReversalEvidenceInspection(
            payment_id=payment.id,
            payment_status=payment.status,
            payment_net_amount=net,
            linked_ledger_entry_ids=tuple(row.id for row in rows if row.id in linked),
            unlinked_candidate_ledger_entry_ids=tuple(
                row.id
                for row in rows
                if row.id not in linked and row.id not in refund_ids
            ),
        )

    @staticmethod
    def reconcile_evidence(
        db: Session,
        payment_id: str,
        *,
        reversal_ledger_entry_id: UUID,
        account_credit_consumption: Decimal = Decimal("0.00"),
        provider_event_id: UUID | None = None,
        commit: bool = True,
    ) -> PaymentReversal:
        initial = get_by_id(db, Payment, payment_id)
        if not initial:
            raise HTTPException(status_code=404, detail="Payment not found")
        if initial.account_id is None:
            raise HTTPException(
                status_code=409,
                detail="Consolidated payment reversals need their own owner",
            )
        lock_account(db, str(initial.account_id))
        payment = lock_for_update(db, Payment, initial.id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        assert payment.account_id is not None
        requested_consumption = round_money(to_decimal(account_credit_consumption))
        existing = db.scalar(
            select(PaymentReversal).where(
                PaymentReversal.ledger_entry_id == reversal_ledger_entry_id
            )
        )
        if existing:
            if existing.payment_id != payment.id:
                raise HTTPException(
                    status_code=409,
                    detail="Reversal ledger evidence belongs to another payment",
                )
            existing_consumption = Decimal("0.00")
            if existing.credit_consumption_ledger_entry_id:
                existing_entry = db.get(
                    LedgerEntry, existing.credit_consumption_ledger_entry_id
                )
                if not existing_entry:
                    raise HTTPException(
                        status_code=409,
                        detail="Linked reversal account-credit evidence is missing",
                    )
                existing_consumption = round_money(to_decimal(existing_entry.amount))
            if (
                existing_consumption != requested_consumption
                or existing.provider_event_id != provider_event_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Payment reversal evidence was reconciled differently",
                )
            return existing
        if payment.status not in {PaymentStatus.failed, PaymentStatus.reversed}:
            raise HTTPException(
                status_code=409,
                detail="Historical payment is not in a reversed-compatible state",
            )
        entry = lock_for_update(db, LedgerEntry, reversal_ledger_entry_id)
        net = round_money(
            to_decimal(payment.amount) - to_decimal(payment.refunded_amount)
        )
        if (
            not entry
            or entry.payment_id != payment.id
            or entry.account_id != payment.account_id
            or entry.source not in {LedgerSource.payment, LedgerSource.refund}
            or entry.entry_type != LedgerEntryType.debit
            or entry.currency != payment.currency
            or round_money(to_decimal(entry.amount)) != net
            or not entry.is_active
        ):
            raise HTTPException(
                status_code=409,
                detail="Selected ledger entry is not exact active reversal evidence",
            )
        origin = PaymentReversalOrigin.manual
        if payment.provider_id is not None:
            event = _validate_reversal_provider_event(
                db,
                payment=payment,
                origin=PaymentReversalOrigin.provider_event,
                provider_event_id=provider_event_id,
            )
            assert event is not None
            if round_money(to_decimal(event.amount)) != net:
                raise HTTPException(
                    status_code=409,
                    detail="Provider event amount does not match reversal evidence",
                )
            origin = PaymentReversalOrigin.provider_event
        elif provider_event_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Manual payment evidence cannot claim a provider event",
            )
        if requested_consumption < 0 or requested_consumption > net:
            raise HTTPException(
                status_code=400,
                detail="Reviewed account-credit consumption is outside the reversal",
            )
        consumption_entry = None
        if requested_consumption > 0:
            consumption_entry = LedgerEntries.create(
                db,
                LedgerEntryCreate(
                    account_id=payment.account_id,
                    entry_type=LedgerEntryType.debit,
                    source=LedgerSource.other,
                    amount=requested_consumption,
                    currency=payment.currency,
                    memo=(
                        f"{_REVERSAL_CONSUMPTION_MEMO_PREFIX} historical "
                        f"reversal ledger {entry.id}"
                    ),
                ),
                commit=False,
            )
        reversal = PaymentReversal(
            payment_id=payment.id,
            provider_event_id=provider_event_id,
            ledger_entry_id=entry.id,
            credit_consumption_ledger_entry_id=(
                consumption_entry.id if consumption_entry else None
            ),
            amount=entry.amount,
            currency=entry.currency,
            origin=origin,
            reason="Historical payment reversal evidence reconciliation",
            preview_fingerprint=None,
        )
        db.add(reversal)
        payment.status = PaymentStatus.reversed
        db.flush()
        for allocation in payment.allocations:
            if not allocation.is_active:
                continue
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                _finalize_invoice_payment_effects(db, invoice)
        from app.services.account_lifecycle import compute_account_status

        compute_account_status(db, str(payment.account_id))
        AuditEvents.stage(
            db,
            AuditEventCreate(
                actor_type=AuditActorType.system,
                action="reconcile_payment_reversal_evidence",
                entity_type="payment",
                entity_id=str(payment.id),
                metadata_={
                    "reversal_id": str(reversal.id),
                    "ledger_entry_id": str(entry.id),
                    "credit_consumption_ledger_entry_id": (
                        str(consumption_entry.id) if consumption_entry else None
                    ),
                    "reviewed_account_credit_consumption": str(requested_consumption),
                    "provider_event_id": (
                        str(provider_event_id) if provider_event_id else None
                    ),
                    "access_consequence": "recheck_after_payment_reversal",
                },
            ),
        )
        if commit:
            db.commit()
            db.refresh(reversal)
        return reversal
