"""Service helpers for billing account web routes."""

from __future__ import annotations

from uuid import UUID

from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.subscriber import SubscriberAccountCreate, SubscriberUpdate
from app.services import billing as billing_service
from app.services import subscriber as subscriber_service
from app.services import web_billing_customers as web_billing_customers_service
from app.services.audit_helpers import build_changes_metadata


def build_accounts_list_data(
    db,
    *,
    page: int,
    per_page: int,
    customer_ref: str | None,
) -> dict[str, object]:
    offset = (page - 1) * per_page
    accounts = []
    total = 0
    if customer_ref:
        subscriber_ids = web_billing_customers_service.subscriber_ids_for_customer(db, customer_ref)
        if subscriber_ids:
            query = db.query(Subscriber).filter(Subscriber.id.in_(subscriber_ids)).order_by(Subscriber.created_at.desc())
            total = query.count()
            accounts = query.offset(offset).limit(per_page).all()
    else:
        accounts = subscriber_service.accounts.list(
            db=db,
            subscriber_id=None,
            reseller_id=None,
            order_by="created_at",
            order_dir="desc",
            limit=per_page,
            offset=offset,
        )
        total = db.query(Subscriber).count()
    total_pages = (total + per_page - 1) // per_page
    return {
        "accounts": accounts,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "customer_ref": customer_ref,
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
        "customer_label": web_billing_customers_service.customer_label(db, customer_ref),
    }


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
        subscribers = web_billing_customers_service.subscribers_for_customer(db, customer_ref)
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
