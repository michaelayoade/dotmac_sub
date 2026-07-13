"""BillingAccount manager: consolidated reseller billing parent.

A BillingAccount sits 1:1 with a Reseller. It owns consolidated payments that
may allocate across any Subscriber under the Reseller. Unallocated payment
surplus accumulates on `BillingAccount.balance`.
"""

from __future__ import annotations

import builtins
import logging
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.billing import (
    BillingAccount,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    BillingAccountCreate,
    BillingAccountStatement,
    BillingAccountStatementPayment,
    BillingAccountStatementSubscriberLine,
    BillingAccountUpdate,
)
from app.services.common import (
    apply_ordering,
    apply_pagination,
    coerce_uuid,
    get_by_id,
    round_money,
    to_decimal,
)
from app.services.response import ListResponseMixin
from app.services.sync_feeds import apply_sync_page, sync_page_response

logger = logging.getLogger(__name__)

_OPEN_INVOICE_STATUSES = (
    InvoiceStatus.issued,
    InvoiceStatus.partially_paid,
    InvoiceStatus.overdue,
)


class BillingAccounts(ListResponseMixin):
    @staticmethod
    def list(
        db: Session,
        *,
        reseller_id: str | None = None,
        is_active: bool | None = True,
        order_by: str = "created_at",
        order_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[BillingAccount]:
        query = db.query(BillingAccount)
        if reseller_id:
            query = query.filter(BillingAccount.reseller_id == coerce_uuid(reseller_id))
        if is_active is True:
            query = query.filter(BillingAccount.is_active.is_(True))
        elif is_active is False:
            query = query.filter(BillingAccount.is_active.is_(False))
        query = apply_ordering(
            query,
            order_by,
            order_dir,
            {
                "created_at": BillingAccount.created_at,
                "name": BillingAccount.name,
            },
        )
        return apply_pagination(query, limit, offset).all()

    @staticmethod
    def list_for_sync(
        db: Session,
        *,
        reseller_id: str | None,
        is_active: bool | None,
        updated_since: datetime | None,
        limit: int,
        offset: int,
    ) -> builtins.list[BillingAccount]:
        query = db.query(BillingAccount)
        if reseller_id:
            query = query.filter(BillingAccount.reseller_id == coerce_uuid(reseller_id))
        if is_active is not None:
            query = query.filter(BillingAccount.is_active == is_active)
        return apply_sync_page(
            query,
            BillingAccount,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
        ).all()

    @classmethod
    def sync_list_response(cls, db: Session, **kwargs):
        items = cls.list_for_sync(db, **kwargs)
        return sync_page_response(items, limit=kwargs["limit"], offset=kwargs["offset"])

    @staticmethod
    def count(
        db: Session,
        *,
        reseller_id: str | None = None,
        is_active: bool | None = True,
    ) -> int:
        query = db.query(func.count(BillingAccount.id))
        if reseller_id:
            query = query.filter(BillingAccount.reseller_id == coerce_uuid(reseller_id))
        if is_active is True:
            query = query.filter(BillingAccount.is_active.is_(True))
        elif is_active is False:
            query = query.filter(BillingAccount.is_active.is_(False))
        return int(query.scalar() or 0)

    @staticmethod
    def get(db: Session, billing_account_id: str) -> BillingAccount:
        ba = get_by_id(db, BillingAccount, billing_account_id)
        if not ba:
            raise HTTPException(status_code=404, detail="Billing account not found")
        return ba

    @staticmethod
    def get_for_reseller(db: Session, reseller_id: str) -> BillingAccount:
        """Return the BillingAccount for a Reseller, creating it lazily if absent.

        Resellers created before this feature shipped, or via paths that bypass
        ``create_default_for_reseller``, still get a working billing account.
        """
        reseller = get_by_id(db, Reseller, reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        ba = (
            db.query(BillingAccount)
            .filter(BillingAccount.reseller_id == reseller.id)
            .first()
        )
        if ba is None:
            ba = BillingAccounts.create_default_for_reseller(db, str(reseller.id))
        return ba

    @staticmethod
    def create_default_for_reseller(db: Session, reseller_id: str) -> BillingAccount:
        reseller = get_by_id(db, Reseller, reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        existing = (
            db.query(BillingAccount)
            .filter(BillingAccount.reseller_id == reseller.id)
            .first()
        )
        if existing:
            return existing
        ba = BillingAccount(
            reseller_id=reseller.id,
            name=reseller.name,
        )
        db.add(ba)
        db.flush()
        return ba

    @staticmethod
    def create(db: Session, payload: BillingAccountCreate) -> BillingAccount:
        reseller = get_by_id(db, Reseller, payload.reseller_id)
        if not reseller:
            raise HTTPException(status_code=404, detail="Reseller not found")
        existing = (
            db.query(BillingAccount)
            .filter(BillingAccount.reseller_id == reseller.id)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail="Reseller already has a billing account",
            )
        ba = BillingAccount(**payload.model_dump())
        db.add(ba)
        db.commit()
        db.refresh(ba)
        return ba

    @staticmethod
    def update(
        db: Session, billing_account_id: str, payload: BillingAccountUpdate
    ) -> BillingAccount:
        ba = BillingAccounts.get(db, billing_account_id)
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            setattr(ba, key, value)
        db.commit()
        db.refresh(ba)
        return ba

    @staticmethod
    def statement(
        db: Session,
        billing_account_id: str,
        *,
        subscribers_limit: int = 25,
        subscribers_offset: int = 0,
        subscriber_search: str | None = None,
        payments_limit: int = 25,
        payments_offset: int = 0,
    ) -> BillingAccountStatement:
        ba = BillingAccounts.get(db, billing_account_id)
        search = (subscriber_search or "").strip()

        # Aggregates: do these in SQL so they don't depend on the page being
        # rendered. The per-subscriber rows below are then a pure page-of-data.
        open_invoice_filter = (
            db.query(Invoice)
            .join(Subscriber, Invoice.account_id == Subscriber.id)
            .filter(Subscriber.reseller_id == ba.reseller_id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .filter(Invoice.balance_due > 0)
        )
        total_outstanding = round_money(
            to_decimal(
                open_invoice_filter.with_entities(
                    func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00"))
                ).scalar()
                or Decimal("0.00")
            )
        )
        subscribers_total = int(
            open_invoice_filter.with_entities(
                func.count(func.distinct(Subscriber.id))
            ).scalar()
            or 0
        )

        subscriber_rows_query = (
            db.query(
                Subscriber.id,
                Subscriber.first_name,
                Subscriber.last_name,
                Subscriber.display_name,
                Subscriber.company_name,
                func.count(Invoice.id).label("open_invoice_count"),
                func.coalesce(func.sum(Invoice.balance_due), Decimal("0.00")).label(
                    "open_balance"
                ),
            )
            .join(Invoice, Invoice.account_id == Subscriber.id)
            .filter(Subscriber.reseller_id == ba.reseller_id)
            .filter(Invoice.is_active.is_(True))
            .filter(Invoice.status.in_(_OPEN_INVOICE_STATUSES))
            .filter(Invoice.balance_due > 0)
        )
        if search:
            pattern = f"%{search}%"
            subscriber_rows_query = subscriber_rows_query.filter(
                or_(
                    Subscriber.first_name.ilike(pattern),
                    Subscriber.last_name.ilike(pattern),
                    Subscriber.display_name.ilike(pattern),
                    Subscriber.company_name.ilike(pattern),
                    Subscriber.email.ilike(pattern),
                    Subscriber.subscriber_number.ilike(pattern),
                )
            )

        # Per-subscriber open balance (paginated page)
        rows = (
            subscriber_rows_query.group_by(
                Subscriber.id,
                Subscriber.first_name,
                Subscriber.last_name,
                Subscriber.display_name,
                Subscriber.company_name,
            )
            .order_by(Subscriber.first_name.asc(), Subscriber.last_name.asc())
            .limit(subscribers_limit)
            .offset(subscribers_offset)
            .all()
        )
        subscribers: builtins.list[BillingAccountStatementSubscriberLine] = []
        for row in rows:
            name = (
                row.display_name
                or row.company_name
                or f"{row.first_name} {row.last_name}".strip()
                or str(row.id)
            )
            open_balance = round_money(to_decimal(row.open_balance))
            subscribers.append(
                BillingAccountStatementSubscriberLine(
                    subscriber_id=row.id,
                    subscriber_name=name,
                    open_invoice_count=int(row.open_invoice_count or 0),
                    open_balance=open_balance,
                )
            )

        # Recent consolidated payments for this billing account (paginated page)
        payments_total = int(
            db.query(func.count(Payment.id))
            .filter(Payment.billing_account_id == ba.id)
            .filter(Payment.is_active.is_(True))
            .scalar()
            or 0
        )
        payment_rows = (
            db.query(
                Payment.id,
                Payment.amount,
                Payment.currency,
                Payment.paid_at,
                Payment.memo,
                func.coalesce(
                    func.sum(PaymentAllocation.amount), Decimal("0.00")
                ).label("allocated_total"),
            )
            .outerjoin(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
            .filter(Payment.billing_account_id == ba.id)
            .filter(Payment.is_active.is_(True))
            .group_by(Payment.id)
            .order_by(Payment.created_at.desc())
            .limit(payments_limit)
            .offset(payments_offset)
            .all()
        )
        recent_payments: builtins.list[BillingAccountStatementPayment] = []
        for prow in payment_rows:
            amount = round_money(to_decimal(prow.amount))
            allocated = round_money(to_decimal(prow.allocated_total))
            recent_payments.append(
                BillingAccountStatementPayment(
                    payment_id=prow.id,
                    amount=amount,
                    currency=prow.currency,
                    paid_at=prow.paid_at,
                    memo=prow.memo,
                    allocated_total=allocated,
                    unallocated_amount=round_money(amount - allocated),
                )
            )

        from app.schemas.billing import BillingAccountRead

        return BillingAccountStatement(
            billing_account=BillingAccountRead.model_validate(ba),
            subscribers=subscribers,
            subscribers_total=subscribers_total,
            recent_payments=recent_payments,
            recent_payments_total=payments_total,
            total_outstanding=total_outstanding,
            unallocated_balance=round_money(to_decimal(ba.balance)),
        )

    @staticmethod
    def list_member_subscriber_ids(
        db: Session, billing_account_id: str
    ) -> builtins.list[str]:
        ba = BillingAccounts.get(db, billing_account_id)
        rows = db.execute(
            select(Subscriber.id).where(Subscriber.reseller_id == ba.reseller_id)
        ).all()
        return [str(r[0]) for r in rows]

    @staticmethod
    def credit_balance(
        db: Session, billing_account_id: str, amount: Decimal
    ) -> BillingAccount:
        """Increment the unallocated balance on a billing account."""
        ba = BillingAccounts.get(db, billing_account_id)
        ba.balance = round_money(to_decimal(ba.balance) + to_decimal(amount))
        ba.updated_at = datetime.now(UTC)
        db.flush()
        return ba

    @staticmethod
    def debit_balance(
        db: Session, billing_account_id: str, amount: Decimal
    ) -> BillingAccount:
        """Decrement the unallocated balance on a billing account."""
        ba = BillingAccounts.get(db, billing_account_id)
        new_balance = round_money(to_decimal(ba.balance) - to_decimal(amount))
        if new_balance < 0:
            raise HTTPException(
                status_code=400, detail="Insufficient billing account balance"
            )
        ba.balance = new_balance
        ba.updated_at = datetime.now(UTC)
        db.flush()
        return ba


billing_accounts = BillingAccounts()
