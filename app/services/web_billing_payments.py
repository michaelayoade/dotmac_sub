"""Service helpers for admin billing payment routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.billing import Payment, PaymentMethod, PaymentMethodType, PaymentStatus
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.billing import PaymentCreate, PaymentMethodCreate, PaymentUpdate
from app.services import billing as billing_service
from app.services import settings_spec
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service

logger = logging.getLogger(__name__)


def _parse_uuid(value: str | None, field: str) -> UUID:
    """Parse a string to UUID, raising ValueError if missing or invalid."""
    if not value:
        raise ValueError(f"{field} is required")
    return UUID(value)


def _parse_decimal(
    value: str | None, field: str, default: Decimal | None = None
) -> Decimal:
    """Parse a string to Decimal, raising ValueError if invalid."""
    if value is None or value == "":
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a valid number") from exc


def payment_primary_invoice_id(payment: Payment | None) -> str | None:
    """Return the invoice_id from the earliest allocation on a payment."""
    if not payment or not payment.allocations:
        return None
    allocation = min(
        payment.allocations,
        key=lambda entry: entry.created_at or datetime.min.replace(tzinfo=UTC),
    )
    return str(allocation.invoice_id)


def _payment_method_type_options() -> list[str]:
    """Return allowed payment method type values from settings."""
    spec = settings_spec.get_spec(SettingDomain.billing, "default_payment_method_type")
    if not spec or not spec.allowed:
        return []
    return sorted(spec.allowed)


def resolve_payment_method_id(
    db: Session, account_id: UUID, selection: str | None
) -> UUID | None:
    """Resolve a payment-method selector to a PaymentMethod UUID.

    *selection* may be:
    - ``None`` / empty -> ``None``
    - ``"id:<uuid>"`` -> use that UUID directly
    - ``"type:<method_type>"`` -> find-or-create by type for the account
    - a bare UUID string -> use that UUID directly
    """
    if not selection:
        return None
    if selection.startswith("id:"):
        return UUID(selection.split(":", 1)[1])
    if selection.startswith("type:"):
        method_type = selection.split(":", 1)[1]
        allowed = _payment_method_type_options()
        if allowed and method_type not in allowed:
            raise ValueError("payment_method_type is invalid")
        method_type_enum = PaymentMethodType(method_type)
        stmt = (
            select(PaymentMethod)
            .where(PaymentMethod.account_id == account_id)
            .where(PaymentMethod.method_type == method_type_enum)
            .where(PaymentMethod.is_active.is_(True))
            .order_by(PaymentMethod.created_at.desc())
            .limit(1)
        )
        method = db.scalars(stmt).first()
        if method:
            return method.id
        label = method_type.replace("_", " ").title()
        payload = PaymentMethodCreate(
            account_id=account_id,
            method_type=method_type_enum,
            label=label,
        )
        method = cast(PaymentMethod, billing_service.payment_methods.create(db, payload))
        return method.id
    return UUID(selection)


def build_payment_detail_data(
    db: Session,
    *,
    payment_id: str,
) -> dict[str, object] | None:
    """Load payment and derived data for the detail page.

    Returns ``None`` when the payment cannot be found.
    """
    payment = billing_service.payments.get(db=db, payment_id=payment_id)
    if not payment:
        return None
    return {
        "payment": payment,
        "primary_invoice_id": payment_primary_invoice_id(payment),
        "active_page": "payments",
        "active_menu": "billing",
    }


def build_payment_edit_data(
    db: Session,
    *,
    payment_id: str,
) -> dict[str, object] | None:
    """Load payment and dependencies for the edit form.

    Returns ``None`` when the payment cannot be found.
    """
    from app.services import web_billing_payment_forms as forms_svc

    payment = billing_service.payments.get(db=db, payment_id=payment_id)
    if not payment:
        return None
    selected_account = payment.account
    primary_inv_id = payment_primary_invoice_id(payment)
    if not selected_account and payment.account_id:
        try:
            selected_account = subscriber_service.accounts.get(
                db=db, account_id=str(payment.account_id)
            )
        except Exception:
            selected_account = None
    deps = forms_svc.load_edit_dependencies(
        db,
        payment=payment,
        selected_account=selected_account,
    )
    return {
        "payment": payment,
        "selected_account": selected_account,
        "primary_invoice_id": primary_inv_id,
        "deps": deps,
    }


def build_create_payload(
    *,
    account_id,
    collection_account_id: str | None,
    amount,
    currency: str,
    status: str | None,
    memo: str | None,
    invoice_id: str | None,
) -> PaymentCreate:
    """Build payment-create payload including optional allocation."""
    allocations = None
    if invoice_id:
        from app.schemas.billing import PaymentAllocationApply

        allocations = [
            PaymentAllocationApply(
                invoice_id=UUID(invoice_id),
                amount=amount,
            )
        ]
    return PaymentCreate(
        account_id=account_id,
        collection_account_id=UUID(collection_account_id) if collection_account_id else None,
        amount=amount,
        currency=currency.strip().upper(),
        status=PaymentStatus(status) if status else PaymentStatus.pending,
        memo=memo.strip() if memo else None,
        allocations=allocations,
    )


def build_update_payload(
    *,
    account_id,
    payment_method_id,
    amount,
    currency: str,
    status: str | None,
    memo: str | None,
) -> PaymentUpdate:
    """Build payment-update payload."""
    return PaymentUpdate(
        account_id=account_id,
        payment_method_id=payment_method_id,
        amount=amount,
        currency=currency.strip().upper(),
        status=PaymentStatus(status) if status else PaymentStatus.pending,
        memo=memo.strip() if memo else None,
    )


def update_invoice_allocation_if_changed(
    db: Session,
    *,
    payment_obj,
    current_invoice_id: str | None,
    requested_invoice_id: str | None,
) -> bool:
    """Replace allocations when requested invoice differs from current."""
    invoice_changed = bool(requested_invoice_id and requested_invoice_id != current_invoice_id)
    if not invoice_changed:
        return False
    for alloc in list(payment_obj.allocations):
        db.delete(alloc)
    db.flush()
    from app.models.billing import PaymentAllocation

    db.add(
        PaymentAllocation(
            payment_id=payment_obj.id,
            invoice_id=UUID(requested_invoice_id),
            amount=payment_obj.amount,
        )
    )
    db.commit()
    db.refresh(payment_obj)
    return True


def parse_import_date(date_str: str | None) -> datetime | None:
    """Parse import date in ISO/common formats."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def import_payments(db: Session, payments_data: list[dict], default_currency: str) -> tuple[int, list[str]]:
    """Import payments payload list; return imported count and error messages."""
    imported_count = 0
    errors: list[str] = []
    for idx, row in enumerate(payments_data):
        try:
            account_id = None
            account_number = row.get("account_number")
            account_id_str = row.get("account_id")
            if account_number:
                account = db.scalars(
                    select(Subscriber).where(
                        Subscriber.account_number == str(account_number)
                    )
                ).first()
                if not account:
                    errors.append(f"Row {idx + 1}: Account not found: {account_number}")
                    continue
                account_id = account.id
            elif account_id_str:
                try:
                    account_id = UUID(account_id_str)
                except ValueError:
                    errors.append(f"Row {idx + 1}: Invalid account_id format")
                    continue
            else:
                errors.append(f"Row {idx + 1}: Missing account identifier")
                continue

            try:
                amount = Decimal(str(row.get("amount", "0")))
                if amount <= 0:
                    errors.append(f"Row {idx + 1}: Amount must be positive")
                    continue
            except (ValueError, InvalidOperation):
                errors.append(f"Row {idx + 1}: Invalid amount")
                continue

            paid_at = parse_import_date(row.get("date"))
            currency = row.get("currency", default_currency) or default_currency
            reference = row.get("reference", "")
            payload = PaymentCreate(
                account_id=account_id,
                amount=amount,
                currency=currency.upper(),
                status=PaymentStatus.succeeded,
                memo=f"Imported payment{': ' + reference if reference else ''}",
            )
            payment = billing_service.payments.create(db, payload)
            if paid_at:
                payment.paid_at = paid_at
                db.commit()
            imported_count += 1
        except Exception as exc:
            errors.append(f"Row {idx + 1}: {str(exc)}")
            continue
    return imported_count, errors


def build_payments_list_data(
    db: Session,
    *,
    page: int,
    per_page: int,
    customer_ref: str | None,
) -> dict[str, object]:
    """Build list/stat data for payments page."""
    offset = (page - 1) * per_page
    account_ids = []
    customer_filtered = bool(customer_ref)
    if customer_ref:
        account_ids = [
            UUID(item["id"])
            for item in web_billing_customers_service.accounts_for_customer(db, customer_ref)
        ]

    payments: list[Payment] = []
    total = 0
    if account_ids:
        base_stmt = (
            select(Payment)
            .where(Payment.account_id.in_(account_ids))
            .where(Payment.is_active.is_(True))
            .order_by(Payment.created_at.desc())
        )
        total = db.scalar(
            select(func.count()).select_from(base_stmt.subquery())
        ) or 0
        payments = list(db.scalars(base_stmt.offset(offset).limit(per_page)).all())
    elif not customer_filtered:
        payments = list(
            billing_service.payments.list(
            db=db,
            account_id=None,
            invoice_id=None,
            status=None,
            is_active=None,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
            )
        )
        total = db.scalar(
            select(func.count(Payment.id)).where(Payment.is_active.is_(True))
        ) or 0

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=2000,
        offset=0,
    )
    total_balance = sum((getattr(account, "balance", 0) or 0) for account in accounts)
    active_count = sum(
        1
        for account in accounts
        if account.status == SubscriberStatus.active
    )
    suspended_count = sum(
        1
        for account in accounts
        if account.status == SubscriberStatus.suspended
    )

    return {
        "payments": payments,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "total_balance": total_balance,
        "active_count": active_count,
        "suspended_count": suspended_count,
        "customer_ref": customer_ref,
    }


def resolve_default_currency(db: Session) -> str:
    value = settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
    return str(value) if value else "NGN"


def build_import_result_payload(*, imported_count: int, errors: list[str]) -> dict[str, object]:
    return {
        "imported": imported_count,
        "errors": errors[:10] if errors else [],
        "total_errors": len(errors),
    }


def import_template_csv() -> str:
    return """account_number,amount,currency,reference,date
ACC-001,15000,NGN,TRF-001,2024-01-15
ACC-002,25000,NGN,TRF-002,2024-01-16
"""


def process_payment_create(
    db: Session,
    *,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    collection_account_id: str | None,
    memo: str | None,
) -> dict[str, object]:
    """Validate inputs, create a payment, and return result dict."""
    from app.services import web_billing_payment_forms as forms_svc

    resolved_invoice = forms_svc.resolve_invoice(db, invoice_id)
    balance_value: str | None = None
    balance_display: str | None = None
    if resolved_invoice:
        balance_value, balance_display = forms_svc.invoice_balance_info(resolved_invoice)
    resolved_account_id = account_id or (str(resolved_invoice.account_id) if resolved_invoice else None)
    if not resolved_account_id:
        raise ValueError("account_id is required")
    parsed_account_id = _parse_uuid(resolved_account_id, "account_id")
    effective_currency = resolved_invoice.currency if resolved_invoice and resolved_invoice.currency else currency
    parsed_amount = _parse_decimal(amount, "amount")
    payload = build_create_payload(
        account_id=parsed_account_id,
        collection_account_id=collection_account_id,
        amount=parsed_amount,
        currency=effective_currency,
        status=status,
        memo=memo,
        invoice_id=invoice_id,
    )
    payment = billing_service.payments.create(db, payload)
    return {
        "payment": payment,
        "resolved_invoice": resolved_invoice,
        "balance_value": balance_value,
        "balance_display": balance_display,
    }


def process_payment_update(
    db: Session,
    *,
    payment_id: str,
    account_id: str | None,
    amount: str,
    currency: str,
    status: str | None,
    invoice_id: str | None,
    payment_method_id: str | None,
    memo: str | None,
) -> dict[str, object]:
    """Validate inputs, update a payment, and return before/after snapshots."""
    from app.services import web_billing_payment_forms as forms_svc

    before = billing_service.payments.get(db=db, payment_id=payment_id)
    current_invoice_id = payment_primary_invoice_id(before)
    resolved_invoice = forms_svc.resolve_invoice(db, invoice_id)
    resolved_account_id = account_id or (str(resolved_invoice.account_id) if resolved_invoice else None)
    if not resolved_account_id:
        resolved_account_id = str(before.account_id) if before else None
    if not resolved_account_id:
        raise ValueError("account_id is required")
    parsed_account_id = _parse_uuid(resolved_account_id, "account_id")
    effective_currency = resolved_invoice.currency if resolved_invoice and resolved_invoice.currency else currency
    requested_invoice_id = str(resolved_invoice.id) if resolved_invoice else None
    invoice_changed = requested_invoice_id and requested_invoice_id != current_invoice_id
    payload = build_update_payload(
        account_id=parsed_account_id,
        payment_method_id=resolve_payment_method_id(db, parsed_account_id, payment_method_id),
        amount=_parse_decimal(amount, "amount"),
        currency=effective_currency,
        status=status,
        memo=memo,
    )
    billing_service.payments.update(db, payment_id, payload)
    if invoice_changed and requested_invoice_id:
        payment_obj = billing_service.payments.get(db=db, payment_id=payment_id)
        update_invoice_allocation_if_changed(
            db,
            payment_obj=payment_obj,
            current_invoice_id=current_invoice_id,
            requested_invoice_id=requested_invoice_id,
        )
    after = billing_service.payments.get(db=db, payment_id=payment_id)
    return {"before": before, "after": after}
