"""Service helpers for billing account web routes."""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber, SubscriberCategory, SubscriberStatus
from app.schemas.subscriber import SubscriberAccountCreate, SubscriberUpdate
from app.services import billing as billing_service
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.audit_helpers import build_changes_metadata, log_audit_event

logger = logging.getLogger(__name__)

_OPEN_BALANCE_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


def build_accounts_list_data(
    db,
    *,
    page: int,
    per_page: int,
    customer_ref: str | None,
    reseller_id: str | None = None,
    search: str | None = None,
    status: str | None = None,
    balance_filter: str | None = None,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    balance_subquery = (
        db.query(
            Invoice.account_id.label("account_id"),
            func.coalesce(func.sum(Invoice.balance_due), 0).label("open_balance"),
        )
        .filter(Invoice.is_active.is_(True))
        .filter(Invoice.status.in_(_OPEN_BALANCE_INVOICE_STATUSES))
        .group_by(Invoice.account_id)
        .subquery()
    )
    balance_value = func.coalesce(balance_subquery.c.open_balance, 0)

    query = db.query(Subscriber).outerjoin(
        balance_subquery, balance_subquery.c.account_id == Subscriber.id
    )
    if customer_ref:
        subscriber_ids = web_billing_customers_service.subscriber_ids_for_customer(
            db, customer_ref
        )
        if subscriber_ids:
            query = query.filter(Subscriber.id.in_(subscriber_ids))
        else:
            query = query.filter(Subscriber.id.is_(None))
    else:
        if reseller_id:
            query = query.filter(Subscriber.reseller_id == UUID(reseller_id))

    normalized_status = (status or "").strip().lower()
    if normalized_status:
        try:
            query = query.filter(
                Subscriber.status == SubscriberStatus(normalized_status)
            )
        except ValueError:
            logger.info(
                "Ignoring unsupported billing account status filter: %s", status
            )

    term = (search or "").strip()
    if term:
        like_term = f"%{term}%"
        query = query.filter(
            or_(
                Subscriber.subscriber_number.ilike(like_term),
                Subscriber.account_number.ilike(like_term),
                Subscriber.first_name.ilike(like_term),
                Subscriber.last_name.ilike(like_term),
                Subscriber.display_name.ilike(like_term),
                Subscriber.company_name.ilike(like_term),
                Subscriber.email.ilike(like_term),
            )
        )

    normalized_balance = (balance_filter or "").strip().lower()
    if normalized_balance == "positive":
        query = query.filter(balance_value > 0)
    elif normalized_balance == "zero":
        query = query.filter(balance_value == 0)
    elif normalized_balance == "credit":
        query = query.filter(balance_value < 0)

    total = query.count()
    stats = (
        query.with_entities(
            func.coalesce(func.sum(balance_value), 0).label("total_balance"),
            func.count(Subscriber.id)
            .filter(Subscriber.status == SubscriberStatus.active)
            .label("active_count"),
            func.count(Subscriber.id)
            .filter(
                Subscriber.status.in_(
                    (SubscriberStatus.blocked, SubscriberStatus.suspended)
                )
            )
            .label("suspended_count"),
        ).one()
        if total
        else None
    )
    rows = (
        query.add_columns(balance_value.label("open_balance"))
        .order_by(Subscriber.created_at.desc())
        .offset(offset)
        .limit(per_page)
        .all()
    )
    accounts = []
    for account, open_balance in rows:
        account.balance = Decimal(str(open_balance or 0))
        accounts.append(account)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "accounts": accounts,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "customer_ref": customer_ref,
        "reseller_id": reseller_id,
        "search": search or "",
        "status": normalized_status,
        "balance_filter": normalized_balance,
        "total_balance": Decimal(str(stats.total_balance if stats else 0)),
        "active_count": int(stats.active_count if stats else 0),
        "suspended_count": int(stats.suspended_count if stats else 0),
    }


def build_account_form_data(db, *, customer_ref: str | None) -> dict[str, object]:
    return {
        "resellers": subscriber_service.resellers.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=500,
            offset=0,
        ),
        "tax_rates": billing_service.tax_rates.list(
            db=db,
            is_active=True,
            order_by="name",
            order_dir="asc",
            limit=100,
            offset=0,
        ),
        "customer_ref": customer_ref,
        "customer_label": web_billing_customers_service.customer_label(
            db, customer_ref
        ),
    }


def customer_ref_for_account(account: Subscriber) -> str:
    return (
        f"business:{account.id}"
        if account.category == SubscriberCategory.business
        else f"person:{account.id}"
    )


def build_new_account_form_context(
    db,
    *,
    customer_ref: str | None,
    selected_subscriber_id: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    context = {
        "action_url": "/admin/billing/accounts",
        "form_title": "New Billing Account",
        "submit_label": "Create Account",
        **build_account_form_data(db, customer_ref=customer_ref),
    }
    if selected_subscriber_id:
        context["selected_subscriber_id"] = selected_subscriber_id
    if error:
        context["error"] = error
    return context


def build_edit_account_form_context(
    db,
    *,
    account_id: str,
    reseller_id: str | None = None,
    tax_rate_id: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    account = subscriber_service.accounts.get(db, account_id)
    context = {
        "action_url": f"/admin/billing/accounts/{account_id}/edit",
        "form_title": "Edit Billing Account",
        "submit_label": "Update Account",
        "account": account,
        "selected_subscriber_id": str(account.id),
        "selected_reseller_id": reseller_id
        or (str(account.reseller_id) if account.reseller_id else ""),
        "selected_tax_rate_id": tax_rate_id
        or (str(account.tax_rate_id) if account.tax_rate_id else ""),
        **build_account_form_data(db, customer_ref=customer_ref_for_account(account)),
    }
    if error:
        context["error"] = error
    return context


def create_account_from_form(
    db,
    *,
    subscriber_id: str | None,
    customer_ref: str | None,
    reseller_id: str | None,
    tax_rate_id: str | None,
    account_number: str | None,
    status: str | None,
    notes: str | None,
):
    resolved_subscriber_id = subscriber_id
    if not resolved_subscriber_id and customer_ref:
        subscribers = web_billing_customers_service.subscribers_for_customer(
            db, customer_ref
        )
        if len(subscribers) == 1:
            resolved_subscriber_id = subscribers[0]["id"]
        elif len(subscribers) > 1:
            raise ValueError("Multiple subscribers found; please choose one.")
    if not resolved_subscriber_id:
        raise ValueError("subscriber_id is required")

    payload = SubscriberAccountCreate(
        subscriber_id=UUID(resolved_subscriber_id),
        reseller_id=UUID(reseller_id) if reseller_id else None,
        account_number=account_number.strip() if account_number else None,
        notes=notes.strip() if notes else None,
    )
    account = subscriber_service.accounts.create(db, payload)
    if tax_rate_id or status:
        resolved_status: SubscriberStatus | None = None
        if status:
            try:
                resolved_status = SubscriberStatus(status)
            except ValueError as exc:
                allowed = ", ".join(s.value for s in SubscriberStatus)
                raise ValueError(f"Invalid status. Allowed: {allowed}") from exc
        subscriber_service.subscribers.update(
            db=db,
            subscriber_id=str(account.id),
            payload=SubscriberUpdate(
                tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
                status=resolved_status,
            ),
        )
    return account, resolved_subscriber_id


def create_account_from_form_with_metadata(
    db,
    *,
    subscriber_id: str | None,
    customer_ref: str | None,
    reseller_id: str | None,
    tax_rate_id: str | None,
    account_number: str | None,
    status: str | None,
    notes: str | None,
):
    account, resolved_subscriber_id = create_account_from_form(
        db,
        subscriber_id=subscriber_id,
        customer_ref=customer_ref,
        reseller_id=reseller_id,
        tax_rate_id=tax_rate_id,
        account_number=account_number,
        status=status,
        notes=notes,
    )
    metadata = {
        "account_number": account.account_number,
        "subscriber_id": str(account.id),
        "reseller_id": reseller_id or None,
    }
    return account, resolved_subscriber_id, metadata


def create_account_from_form_web(
    db,
    *,
    request,
    actor_id: str | None,
    subscriber_id: str | None,
    customer_ref: str | None,
    reseller_id: str | None,
    tax_rate_id: str | None,
    account_number: str | None,
    status: str | None,
    notes: str | None,
):
    account, selected_subscriber_id, metadata_payload = (
        create_account_from_form_with_metadata(
            db,
            subscriber_id=subscriber_id,
            customer_ref=customer_ref,
            reseller_id=reseller_id,
            tax_rate_id=tax_rate_id,
            account_number=account_number,
            status=status,
            notes=notes,
        )
    )
    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="subscriber_account",
        entity_id=str(account.id),
        actor_id=actor_id,
        metadata=metadata_payload,
    )
    return account, selected_subscriber_id


def update_account_from_form(
    db,
    *,
    account_id: str,
    reseller_id: str | None,
    tax_rate_id: str | None,
    account_number: str | None,
    status: str | None,
    notes: str | None,
):
    resolved_status: SubscriberStatus | None = None
    if status:
        try:
            resolved_status = SubscriberStatus(status)
        except ValueError as exc:
            allowed = ", ".join(s.value for s in SubscriberStatus)
            raise ValueError(f"Invalid status. Allowed: {allowed}") from exc

    return subscriber_service.subscribers.update(
        db=db,
        subscriber_id=account_id,
        payload=SubscriberUpdate(
            reseller_id=UUID(reseller_id) if reseller_id else None,
            tax_rate_id=UUID(tax_rate_id) if tax_rate_id else None,
            account_number=account_number.strip() if account_number else None,
            status=resolved_status,
            notes=notes.strip() if notes else None,
        ),
    )


def update_account_from_form_with_metadata(
    db,
    *,
    account_id: str,
    reseller_id: str | None,
    tax_rate_id: str | None,
    account_number: str | None,
    status: str | None,
    notes: str | None,
):
    before = subscriber_service.accounts.get(db, account_id)
    account = update_account_from_form(
        db,
        account_id=account_id,
        reseller_id=reseller_id,
        tax_rate_id=tax_rate_id,
        account_number=account_number,
        status=status,
        notes=notes,
    )
    after = subscriber_service.accounts.get(db, account_id)
    metadata = build_changes_metadata(before, after)
    return account, metadata


def update_account_from_form_web(
    db,
    *,
    request,
    actor_id: str | None,
    account_id: str,
    reseller_id: str | None,
    tax_rate_id: str | None,
    account_number: str | None,
    status: str | None,
    notes: str | None,
):
    account, metadata_payload = update_account_from_form_with_metadata(
        db,
        account_id=account_id,
        reseller_id=reseller_id,
        tax_rate_id=tax_rate_id,
        account_number=account_number,
        status=status,
        notes=notes,
    )
    log_audit_event(
        db=db,
        request=request,
        action="update",
        entity_type="subscriber_account",
        entity_id=account_id,
        actor_id=actor_id,
        metadata=metadata_payload,
    )
    return account


def build_account_detail_data(db, *, account_id: str) -> dict[str, object]:
    account = subscriber_service.accounts.get(db, account_id)
    invoices = billing_service.invoices.list(
        db=db,
        account_id=account_id,
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    return {"account": account, "invoices": invoices}
