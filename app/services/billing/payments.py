"""Payment and payment method management services."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.billing import (
    BankAccount,
    BankAccountType,
    CollectionAccount,
    CreditNote,
    CreditNoteLine,
    CreditNoteStatus,
    Invoice,
    InvoiceStatus,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentChannel,
    PaymentChannelAccount,
    PaymentMethod,
    PaymentMethodType,
    PaymentStatus,
)
from app.models.domain_settings import SettingDomain
from app.schemas.billing import (
    BankAccountCreate,
    BankAccountUpdate,
    CollectionAccountCreate,
    CollectionAccountUpdate,
    PaymentAllocationCreate,
    PaymentChannelAccountCreate,
    PaymentChannelAccountUpdate,
    PaymentChannelCreate,
    PaymentChannelUpdate,
    PaymentCreate,
    PaymentMethodCreate,
    PaymentMethodUpdate,
    PaymentUpdate,
)
from app.services import settings_spec
from app.services.billing._common import (
    _recalculate_invoice_totals,
    _resolve_collection_account,
    _resolve_payment_channel,
    _validate_account,
    _validate_collection_account,
    _validate_invoice_currency,
    _validate_payment_channel,
    _validate_payment_linkages,
    _validate_payment_provider,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    get_by_id,
    round_money,
    validate_enum,
)
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.response import ListResponseMixin


class PaymentMethods(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentMethodCreate):
        _validate_account(db, str(payload.account_id))
        if payload.payment_channel_id:
            _validate_payment_channel(db, str(payload.payment_channel_id))
        if payload.is_default:
            db.query(PaymentMethod).filter(
                PaymentMethod.account_id == payload.account_id,
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
            {"created_at": PaymentMethod.created_at, "method_type": PaymentMethod.method_type},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, method_id: str, payload: PaymentMethodUpdate):
        method = get_by_id(db, PaymentMethod, method_id)
        if not method:
            raise HTTPException(status_code=404, detail="Payment method not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = data.get("account_id", method.account_id)
        _validate_account(db, str(account_id))
        if "payment_channel_id" in data:
            _validate_payment_channel(db, str(data["payment_channel_id"]) if data["payment_channel_id"] else None)
        if data.get("is_default"):
            db.query(PaymentMethod).filter(
                PaymentMethod.account_id == account_id,
                PaymentMethod.id != method.id,
                PaymentMethod.is_default.is_(True),
            ).update({"is_default": False})
        for key, value in data.items():
            setattr(method, key, value)
        db.commit()
        db.refresh(method)
        return method

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
        payment_method_id = data.get("payment_method_id", bank_account.payment_method_id)
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

    Args:
        db: Database session
        payment: The payment record
        invoice: Optional invoice (for allocation entries)
        allocation_amount: Amount allocated to the invoice (if different from payment amount)

    Returns:
        The created ledger entry, or None if entry already exists
    """
    # Idempotency check: skip if ledger entry already exists for this payment/invoice
    existing_entry = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.payment_id == payment.id)
        .filter(LedgerEntry.invoice_id == (invoice.id if invoice else None))
        .filter(LedgerEntry.source == LedgerSource.payment)
        .first()
    )
    if existing_entry:
        return existing_entry

    amount = allocation_amount if allocation_amount is not None else payment.amount
    memo = f"Payment {payment.id}"
    if invoice:
        memo = f"Payment {payment.id} applied to Invoice {invoice.invoice_number or invoice.id}"

    entry = LedgerEntry(
        account_id=payment.account_id,
        invoice_id=invoice.id if invoice else None,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=round_money(Decimal(str(amount))),
        currency=payment.currency or "NGN",
        memo=memo,
    )
    db.add(entry)
    return entry


def _create_refund_ledger_entry(
    db: Session,
    payment: Payment,
    refund_amount: Decimal,
    memo: str | None = None,
) -> LedgerEntry:
    """Create a ledger entry for a refund (reverses a payment credit).

    Args:
        db: Database session
        payment: The original payment being refunded
        refund_amount: Amount being refunded
        memo: Optional memo for the entry

    Returns:
        The created ledger entry
    """
    entry = LedgerEntry(
        account_id=payment.account_id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.refund,
        amount=round_money(refund_amount),
        currency=payment.currency or "NGN",
        memo=memo or f"Refund of Payment {payment.id}",
    )
    db.add(entry)
    return entry


def _primary_allocation_invoice_id(payment: Payment) -> str | None:
    if not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


class Payments(ListResponseMixin):
    @staticmethod
    def _auto_allocate(db: Session, payment: Payment) -> list[PaymentAllocation]:
        """Auto-allocate payment to oldest unpaid invoices.

        Returns:
            List of created allocations
        """
        remaining = round_money(Decimal(str(payment.amount or Decimal("0.00"))))
        if remaining <= 0:
            return []
        invoices = (
            db.query(Invoice)
            .filter(Invoice.account_id == payment.account_id)
            .filter(Invoice.is_active.is_(True))
            .filter(
                Invoice.status.in_(
                    [InvoiceStatus.issued, InvoiceStatus.partially_paid, InvoiceStatus.overdue]
                )
            )
            .filter(Invoice.balance_due > 0)
            .order_by(Invoice.due_at.asc().nulls_last(), Invoice.created_at.asc())
            .all()
        )
        allocations: list[PaymentAllocation] = []
        for invoice in invoices:
            if invoice.currency != payment.currency:
                continue
            amount = min(remaining, round_money(Decimal(str(invoice.balance_due))))
            if amount <= 0:
                continue

            # Idempotency check: skip if allocation already exists
            existing_allocation = (
                db.query(PaymentAllocation)
                .filter(PaymentAllocation.payment_id == payment.id)
                .filter(PaymentAllocation.invoice_id == invoice.id)
                .first()
            )
            if existing_allocation:
                # Allocation already exists - use its amount and skip creating
                remaining = round_money(remaining - round_money(Decimal(str(existing_allocation.amount))))
                allocations.append(existing_allocation)
                continue

            allocation = PaymentAllocation(
                payment_id=payment.id,
                invoice_id=invoice.id,
                amount=amount,
            )
            db.add(allocation)
            allocations.append(allocation)

            # Create ledger entry for this allocation
            _create_payment_ledger_entry(db, payment, invoice, amount)

            remaining = round_money(remaining - amount)
            if remaining <= 0:
                break

        # If there's remaining unallocated amount, create a credit ledger entry
        if remaining > 0:
            _create_payment_ledger_entry(db, payment, None, remaining)

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
        remaining = round_money(Decimal(str(payment.amount or Decimal("0.00"))))
        for allocation in allocations:
            if allocation.amount > remaining:
                raise HTTPException(
                    status_code=400, detail="Allocation amount exceeds payment amount"
                )
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if not invoice:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if str(invoice.account_id) != str(payment.account_id):
                raise HTTPException(
                    status_code=400, detail="Invoice does not belong to account"
                )
            _validate_invoice_currency(invoice, payment.currency)

            # Idempotency check: skip if allocation already exists
            existing = (
                db.query(PaymentAllocation)
                .filter(PaymentAllocation.payment_id == payment.id)
                .filter(PaymentAllocation.invoice_id == allocation.invoice_id)
                .first()
            )
            if existing:
                created.append(existing)
                remaining = round_money(remaining - existing.amount)
                continue

            entry = PaymentAllocation(
                payment_id=payment.id,
                invoice_id=allocation.invoice_id,
                amount=allocation.amount,
                memo=allocation.memo,
            )
            db.add(entry)
            created.append(entry)

            # Create ledger entry for this allocation
            _create_payment_ledger_entry(db, payment, invoice, allocation.amount)

            remaining = round_money(remaining - allocation.amount)

        # If there's remaining unallocated amount, create a credit ledger entry
        if remaining > 0:
            _create_payment_ledger_entry(db, payment, None, remaining)

        return created

    @staticmethod
    def create(db: Session, payload: PaymentCreate):
        if payload.amount is not None and payload.amount <= 0:
            raise HTTPException(status_code=400, detail="Payment amount must be greater than 0")
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
                data["status"] = validate_enum(
                    default_status, PaymentStatus, "status"
                )
        _validate_payment_linkages(
            db,
            str(payload.account_id),
            None,
            str(payload.payment_method_id) if payload.payment_method_id else None,
        )
        _validate_payment_provider(db, str(payload.provider_id) if payload.provider_id else None)
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
            str(payload.collection_account_id) if payload.collection_account_id else None,
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
        payment = Payment(**data)
        db.add(payment)
        db.flush()
        allocation_creates: list[PaymentAllocationCreate] = []
        if payload.allocations:
            allocation_creates = [
                PaymentAllocationCreate(
                    payment_id=payment.id,
                    invoice_id=allocation.invoice_id,
                    amount=allocation.amount,
                    memo=allocation.memo,
                )
                for allocation in payload.allocations
            ]
        allocations: list[PaymentAllocation]
        if allocation_creates:
            allocations = Payments._create_allocations(db, payment, allocation_creates)
        else:
            allocations = Payments._auto_allocate(db, payment)

        # Tests run with autoflush disabled; make sure allocations/ledger exist in DB
        # before we query them during invoice recalculation.
        db.flush()

        invoices_to_recalculate = {alloc.invoice_id for alloc in allocations}
        for invoice_id in invoices_to_recalculate:
            invoice = get_by_id(db, Invoice, invoice_id)
            if invoice:
                _recalculate_invoice_totals(db, invoice)
                if invoice.status == InvoiceStatus.paid:
                    from app.services import collections as collections_service
                    collections_service.restore_account_services(
                        db, str(invoice.account_id), invoice_id=str(invoice.id)
                    )
        db.commit()
        db.refresh(payment)

        # Emit payment.received event
        allocation_invoice_id = _primary_allocation_invoice_id(payment)
        emit_event(
            db,
            EventType.payment_received,
            {
                "payment_id": str(payment.id),
                "amount": str(payment.amount) if payment.amount else None,
                "currency": payment.currency,
                "invoice_id": allocation_invoice_id,
                "status": payment.status.value if payment.status else None,
            },
            account_id=payment.account_id,
            invoice_id=allocation_invoice_id,
        )

        return payment

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
    ):
        query = db.query(Payment)
        if account_id:
            query = query.filter(Payment.account_id == account_id)
        if invoice_id:
            query = (
                query.join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
                .filter(PaymentAllocation.invoice_id == invoice_id)
            )
        if status:
            query = query.filter(
                Payment.status == validate_enum(status, PaymentStatus, "status")
            )
        if is_active is None:
            query = query.filter(Payment.is_active.is_(True))
        else:
            query = query.filter(Payment.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": Payment.created_at, "paid_at": Payment.paid_at, "status": Payment.status},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, payment_id: str, payload: PaymentUpdate):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        data = payload.model_dump(exclude_unset=True)
        account_id = str(data.get("account_id", payment.account_id))
        payment_method_id = data.get("payment_method_id", payment.payment_method_id)
        explicit_channel = "payment_channel_id" in data
        payment_channel_id = data.get("payment_channel_id") if explicit_channel else None
        collection_account_id = data.get("collection_account_id", payment.collection_account_id)
        provider_id = data.get("provider_id", payment.provider_id)
        _validate_payment_linkages(
            db,
            account_id,
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
        for key, value in data.items():
            setattr(payment, key, value)
        invoice_ids = [
            alloc.invoice_id for alloc in payment.allocations
        ]
        for invoice_id in invoice_ids:
            invoice = get_by_id(db, Invoice, invoice_id)
            if invoice:
                db.flush()
                _recalculate_invoice_totals(db, invoice)
                if invoice.status == InvoiceStatus.paid:
                    from app.services import collections as collections_service
                    collections_service.restore_account_services(
                        db, str(invoice.account_id), invoice_id=str(invoice.id)
                    )
        db.commit()
        db.refresh(payment)
        return payment

    @staticmethod
    def delete(db: Session, payment_id: str):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        payment.is_active = False
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                db.flush()
                _recalculate_invoice_totals(db, invoice)
        db.commit()

    @staticmethod
    def mark_status(db: Session, payment_id: str, status: PaymentStatus | str):
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        previous_status = payment.status
        normalized = validate_enum(status, PaymentStatus, "status")
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid status")
        payment.status = normalized
        if normalized == PaymentStatus.succeeded:
            payment.paid_at = datetime.now(UTC)
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                db.flush()
                _recalculate_invoice_totals(db, invoice)
        if normalized == PaymentStatus.succeeded:
            # Deferred import to avoid circular dependency
            from app.services import collections as collections_service

            collections_service.dunning_workflow.resolve_cases_for_account(
                db,
                str(payment.account_id),
                _primary_allocation_invoice_id(payment),
                commit=False,
            )
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
            if normalized == PaymentStatus.succeeded:
                emit_event(
                    db,
                    EventType.payment_received,
                    payload,
                    account_id=payment.account_id,
                    invoice_id=allocation_invoice_id,
                )
            elif normalized == PaymentStatus.failed:
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

        return payment


class PaymentAllocations(ListResponseMixin):
    @staticmethod
    def create(db: Session, payload: PaymentAllocationCreate):
        payment = db.query(Payment).filter(Payment.id == payload.payment_id).with_for_update().first()
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")
        invoice = get_by_id(db, Invoice, payload.invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if str(invoice.account_id) != str(payment.account_id):
            raise HTTPException(status_code=400, detail="Invoice does not belong to account")
        _validate_invoice_currency(invoice, payment.currency)
        # Idempotency check: return existing allocation for same (payment_id, invoice_id)
        existing = db.query(PaymentAllocation).filter(
            PaymentAllocation.payment_id == payment.id,
            PaymentAllocation.invoice_id == payload.invoice_id,
        ).first()
        if existing:
            return existing
        allocated_amount = (
            db.query(PaymentAllocation)
            .filter(PaymentAllocation.payment_id == payment.id)
            .with_entities(func.coalesce(func.sum(PaymentAllocation.amount), 0))
            .scalar()
        )
        if payload.amount + allocated_amount > payment.amount:
            raise HTTPException(status_code=400, detail="Allocation exceeds payment amount")
        allocation = PaymentAllocation(**payload.model_dump())
        db.add(allocation)
        db.flush()
        _create_payment_ledger_entry(db, payment, invoice, allocation.amount)
        _recalculate_invoice_totals(db, invoice)
        if invoice.status == InvoiceStatus.paid:
            from app.services import collections as collections_service
            collections_service.restore_account_services(
                db, str(invoice.account_id), invoice_id=str(invoice.id)
            )
        db.commit()
        db.refresh(allocation)
        return allocation

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
            {"created_at": PaymentAllocation.created_at, "amount": PaymentAllocation.amount},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def delete(db: Session, allocation_id: str):
        allocation = get_by_id(db, PaymentAllocation, allocation_id)
        if not allocation:
            raise HTTPException(status_code=404, detail="Payment allocation not found")
        invoice = get_by_id(db, Invoice, allocation.invoice_id)
        # Soft-delete corresponding ledger entries
        db.query(LedgerEntry).filter(
            LedgerEntry.payment_id == allocation.payment_id,
            LedgerEntry.invoice_id == allocation.invoice_id,
            LedgerEntry.source == LedgerSource.payment,
        ).update({"is_active": False})
        db.delete(allocation)
        if invoice:
            db.flush()
            _recalculate_invoice_totals(db, invoice)
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
            {"created_at": CollectionAccount.created_at, "name": CollectionAccount.name},
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
        assert channel is not None
        _validate_collection_account(db, str(payload.collection_account_id), payload.currency)
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
            raise HTTPException(status_code=404, detail="Channel account mapping not found")
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
            query = query.filter(PaymentChannelAccount.collection_account_id == collection_account_id)
        if is_active is None:
            query = query.filter(PaymentChannelAccount.is_active.is_(True))
        else:
            query = query.filter(PaymentChannelAccount.is_active == is_active)
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {"created_at": PaymentChannelAccount.created_at, "priority": PaymentChannelAccount.priority},
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def update(db: Session, mapping_id: str, payload: PaymentChannelAccountUpdate):
        mapping = get_by_id(db, PaymentChannelAccount, mapping_id)
        if not mapping:
            raise HTTPException(status_code=404, detail="Channel account mapping not found")
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
            raise HTTPException(status_code=404, detail="Channel account mapping not found")
        mapping.is_active = False
        db.commit()


class Refunds:
    """Service for processing payment refunds.

    Refunds create credit ledger entries and update payment status.
    Can optionally create credit notes for partial refunds.
    """

    @staticmethod
    def process_refund(
        db: Session,
        payment_id: str,
        refund_amount: Decimal | None = None,
        reason: str | None = None,
        create_credit_note: bool = False,
    ) -> Payment:
        """Process a refund for a payment.

        Args:
            db: Database session
            payment_id: The payment to refund
            refund_amount: Amount to refund (defaults to full payment amount)
            reason: Reason for the refund
            create_credit_note: Whether to create a credit note

        Returns:
            The updated payment
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")

        if payment.status == PaymentStatus.refunded:
            raise HTTPException(status_code=400, detail="Payment already fully refunded")

        if payment.status not in (PaymentStatus.succeeded, PaymentStatus.partially_refunded):
            raise HTTPException(
                status_code=400,
                detail="Only succeeded or partially refunded payments can be refunded"
            )

        # Calculate already refunded amount from ledger entries
        already_refunded = (
            db.query(func.coalesce(func.sum(LedgerEntry.amount), Decimal("0.00")))
            .filter(
                LedgerEntry.payment_id == payment.id,
                LedgerEntry.source == LedgerSource.refund,
            )
            .scalar()
        )
        refundable_amount = payment.amount - already_refunded

        amount_to_refund = refund_amount or refundable_amount
        if amount_to_refund > refundable_amount:
            raise HTTPException(
                status_code=400,
                detail=f"Refund amount exceeds refundable balance ({refundable_amount})"
            )

        # Create refund ledger entry
        _create_refund_ledger_entry(
            db,
            payment,
            amount_to_refund,
            reason or f"Refund: {payment_id}"
        )

        # Update payment status - full refund if this refund exhausts remaining balance
        is_full_refund = amount_to_refund == refundable_amount
        if is_full_refund:
            payment.status = PaymentStatus.refunded
        else:
            payment.status = PaymentStatus.partially_refunded

        # Recalculate invoice totals for all allocated invoices
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                # For full refunds, remove the allocation effect
                if is_full_refund:
                    db.delete(allocation)
                db.flush()
                _recalculate_invoice_totals(db, invoice)

        # Create credit note if requested
        if create_credit_note and payment.allocations:
            Refunds._create_credit_note(db, payment, amount_to_refund, reason)

        db.commit()
        db.refresh(payment)

        # Emit payment.refunded event
        emit_event(
            db,
            EventType.payment_refunded,
            {
                "payment_id": str(payment.id),
                "refund_amount": str(amount_to_refund),
                "currency": payment.currency,
                "reason": reason,
                "is_full_refund": is_full_refund,
            },
            account_id=payment.account_id,
        )

        return payment

    @staticmethod
    def _create_credit_note(
        db: Session,
        payment: Payment,
        amount: Decimal,
        reason: str | None,
    ) -> CreditNote | None:
        """Create a credit note for a refund using the proper CreditNote model.

        Credit notes have positive amounts and can be applied to future invoices.
        """
        if not payment.allocations:
            return None

        # Get the first allocated invoice as reference for linking
        reference_invoice = get_by_id(db, Invoice, payment.allocations[0].invoice_id)

        credit_note = CreditNote(
            account_id=payment.account_id,
            invoice_id=reference_invoice.id if reference_invoice else None,
            status=CreditNoteStatus.issued,
            currency=payment.currency or "NGN",
            subtotal=round_money(amount),
            tax_total=Decimal("0.00"),
            total=round_money(amount),
            applied_total=Decimal("0.00"),
            memo=reason or f"Credit note for refund of payment {payment.id}",
        )
        db.add(credit_note)
        db.flush()

        # Create credit note line item
        line = CreditNoteLine(
            credit_note_id=credit_note.id,
            description=reason or f"Refund of payment {payment.id}",
            quantity=Decimal("1.000"),
            unit_price=round_money(amount),
            amount=round_money(amount),
        )
        db.add(line)

        # Create credit ledger entry
        entry = LedgerEntry(
            account_id=payment.account_id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.credit_note,
            amount=round_money(amount),
            currency=payment.currency or "NGN",
            memo=f"Credit note {credit_note.id}",
        )
        db.add(entry)

        return credit_note

    @staticmethod
    def reverse_payment(
        db: Session,
        payment_id: str,
        reason: str | None = None,
    ) -> Payment:
        """Reverse a payment entirely (e.g., for chargebacks or bank reversals).

        This marks the payment as failed and recalculates all affected invoices.

        Args:
            db: Database session
            payment_id: The payment to reverse
            reason: Reason for the reversal

        Returns:
            The updated payment
        """
        payment = get_by_id(db, Payment, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found")

        if payment.status == PaymentStatus.failed:
            raise HTTPException(status_code=400, detail="Payment already failed/reversed")

        previous_status = payment.status

        # Create reversal ledger entry if payment was succeeded
        if previous_status == PaymentStatus.succeeded:
            _create_refund_ledger_entry(
                db,
                payment,
                payment.amount,
                reason or f"Payment reversal: {payment_id}"
            )

        # Mark payment as failed
        payment.status = PaymentStatus.failed

        # Recalculate all affected invoices
        for allocation in payment.allocations:
            invoice = get_by_id(db, Invoice, allocation.invoice_id)
            if invoice:
                db.flush()
                _recalculate_invoice_totals(db, invoice)

        db.commit()
        db.refresh(payment)

        # Emit payment.failed event
        emit_event(
            db,
            EventType.payment_failed,
            {
                "payment_id": str(payment.id),
                "amount": str(payment.amount),
                "currency": payment.currency,
                "reason": reason or "payment_reversed",
                "from_status": previous_status.value if previous_status else None,
            },
            account_id=payment.account_id,
        )

        return payment
