"""The admin headline money cards use their own exact invoice cohorts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.billing import Invoice, InvoiceStatus
from app.models.subscriber import Subscriber
from app.services.billing.reporting import BillingReporting, DashboardCollectionsPeriod
from app.services.invoice_collectibility import dashboard_overdue_status_balance
from app.services.web_billing_overview import (
    build_invoice_list_query,
    list_invoices_for_scope,
)


def _invoice(
    account_id: UUID,
    number: str,
    status: InvoiceStatus,
    *,
    balance: str,
    created_at: datetime,
) -> Invoice:
    return Invoice(
        account_id=account_id,
        invoice_number=number,
        status=status,
        currency="NGN",
        subtotal=Decimal("100"),
        tax_total=Decimal("0"),
        total=Decimal("100"),
        balance_due=Decimal(balance),
        issued_at=created_at,
        due_at=created_at + timedelta(days=7),
        created_at=created_at,
    )


def test_collections_count_only_paid_and_partially_paid_invoices_created_this_month(
    db_session: Session, subscriber_account: Subscriber
) -> None:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 11, 1, tzinfo=UTC)
    created = start + timedelta(days=2)
    db_session.add_all(
        [
            _invoice(
                subscriber_account.id,
                "KPI-PAID",
                InvoiceStatus.paid,
                balance="0",
                created_at=created,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-PARTIAL",
                InvoiceStatus.partially_paid,
                balance="60",
                created_at=created,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-ISSUED",
                InvoiceStatus.issued,
                balance="40",
                created_at=created,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-OVERDUE",
                InvoiceStatus.overdue,
                balance="50",
                created_at=created,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-OLD",
                InvoiceStatus.paid,
                balance="0",
                created_at=start - timedelta(days=1),
            ),
        ]
    )
    db_session.flush()

    amount = BillingReporting.get_admin_dashboard_collections(
        db_session, period=DashboardCollectionsPeriod(start=start, end=end)
    )

    assert amount == Decimal("140")


def test_overdue_counts_only_invoices_with_overdue_status_across_months(
    db_session: Session, subscriber_account: Subscriber
) -> None:
    old = datetime(2026, 5, 1, tzinfo=UTC)
    recent = datetime(2026, 10, 1, tzinfo=UTC)
    db_session.add_all(
        [
            _invoice(
                subscriber_account.id,
                "KPI-OLD-OVERDUE",
                InvoiceStatus.overdue,
                balance="80",
                created_at=old,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-NEW-OVERDUE",
                InvoiceStatus.overdue,
                balance="30",
                created_at=recent,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-LATE-ISSUED",
                InvoiceStatus.issued,
                balance="90",
                created_at=old,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-LATE-PARTIAL",
                InvoiceStatus.partially_paid,
                balance="40",
                created_at=old,
            ),
        ]
    )
    db_session.flush()

    assert dashboard_overdue_status_balance(db_session) == Decimal("110")


def test_money_card_drilldowns_use_matching_invoice_status_cohorts(
    db_session: Session, subscriber_account: Subscriber
) -> None:
    created = datetime.now(UTC)
    old = created - timedelta(days=90)
    db_session.add_all(
        [
            _invoice(
                subscriber_account.id,
                "KPI-LIST-PAID",
                InvoiceStatus.paid,
                balance="0",
                created_at=created,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-LIST-PARTIAL",
                InvoiceStatus.partially_paid,
                balance="70",
                created_at=created,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-LIST-OVERDUE",
                InvoiceStatus.overdue,
                balance="80",
                created_at=old,
            ),
            _invoice(
                subscriber_account.id,
                "KPI-LIST-ISSUED",
                InvoiceStatus.issued,
                balance="100",
                created_at=old,
            ),
        ]
    )
    db_session.flush()

    def numbers(status: str) -> set[str]:
        query = build_invoice_list_query(
            account_id=None,
            partner_id=None,
            status=status,
            proforma_only=False,
            customer_ref=None,
            search="KPI-LIST-",
            start_date=None,
            end_date=None,
        )
        return {
            invoice.invoice_number
            for invoice in list_invoices_for_scope(db_session, list_query=query)
        }

    assert numbers("dashboard_collections") == {
        "KPI-LIST-PAID",
        "KPI-LIST-PARTIAL",
    }
    assert numbers("dashboard_overdue") == {"KPI-LIST-OVERDUE"}
