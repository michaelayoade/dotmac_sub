from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.billing import InvoiceStatus, LedgerEntry, LedgerEntryType, LedgerSource
from app.models.catalog import BillingMode, DunningAction, SubscriptionStatus
from app.models.subscriber import AccountStatus
from app.schemas.billing import InvoiceCreate
from app.schemas.collections import (
    DunningActionLogCreate,
    DunningCaseCreate,
    PrepaidEnforcementRunRequest,
)
from app.services import billing as billing_service
from app.services import collections as collections_service


def test_dunning_case_and_action_log(db_session, subscriber_account):
    case = collections_service.dunning_cases.create(
        db_session,
        DunningCaseCreate(account_id=subscriber_account.id),
    )
    log = collections_service.dunning_action_logs.create(
        db_session,
        DunningActionLogCreate(
            case_id=case.id,
            action=DunningAction.notify,
            outcome="queued",
        ),
    )
    items = collections_service.dunning_action_logs.list(
        db_session,
        case_id=case.id,
        invoice_id=None,
        payment_id=None,
        order_by="executed_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert items[0].id == log.id


def test_dunning_case_list_invalid_status(db_session):
    with pytest.raises(HTTPException) as exc:
        collections_service.dunning_cases.list(
            db_session,
            account_id=None,
            status="not_valid",
            order_by="created_at",
            order_dir="desc",
            limit=10,
            offset=0,
        )
    assert exc.value.status_code == 400


def test_prepaid_enforcement_accounts_for_open_invoice_balance(
    db_session, subscriber_account, subscription
):
    subscription.billing_mode = BillingMode.prepaid
    subscriber_account.min_balance = Decimal("7.00")
    subscriber_account.grace_period = 0
    db_session.commit()

    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            status=InvoiceStatus.issued,
            total=Decimal("4.00"),
            balance_due=Decimal("4.00"),
            issued_at=datetime.now(UTC),
        ),
    )
    assert invoice.balance_due == Decimal("4.00")

    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("10.00"),
            currency="NGN",
            memo="Prepaid top-up",
        )
    )
    db_session.commit()

    collections_service.prepaid_enforcement.run(
        db_session,
        PrepaidEnforcementRunRequest(run_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC)),
    )

    db_session.refresh(subscriber_account)
    db_session.refresh(subscription)
    assert subscriber_account.status == AccountStatus.suspended
    assert subscription.status == SubscriptionStatus.suspended
