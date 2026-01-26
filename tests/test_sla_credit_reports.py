from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models.billing import CreditNoteStatus
from app.models.catalog import AccessType, PriceBasis, ServiceType
from app.schemas.billing import InvoiceCreate
from app.schemas.catalog import (
    CatalogOfferCreate,
    SlaProfileCreate,
    SubscriptionCreate,
)
from app.schemas.sla_credit import (
    SlaCreditApplyRequest,
    SlaCreditItemUpdate,
    SlaCreditReportCreate,
)
from app.schemas.tickets import TicketCreate, TicketSlaEventCreate
from app.services import billing as billing_service
from app.services import catalog as catalog_service
from app.services import sla_credit as sla_credit_service
from app.services import tickets as tickets_service


def test_sla_credit_report_apply_flow(db_session, subscriber_account):
    profile = catalog_service.sla_profiles.create(
        db_session,
        SlaProfileCreate(name="Gold", uptime_percent=Decimal("98"), credit_percent=Decimal("10")),
    )
    offer = catalog_service.offers.create(
        db_session,
        CatalogOfferCreate(
            name="Fiber 100",
            code="F100",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            sla_profile_id=profile.id,
        ),
    )
    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=subscriber_account.id,
            offer_id=offer.id,
        ),
    )
    period_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    period_end = datetime(2024, 2, 1, tzinfo=timezone.utc)
    invoice = billing_service.invoices.create(
        db_session,
        InvoiceCreate(
            account_id=subscriber_account.id,
            currency="USD",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("0.00"),
            total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
            billing_period_start=period_start,
            billing_period_end=period_end,
        ),
    )
    ticket = tickets_service.tickets.create(
        db_session,
        TicketCreate(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            title="Outage",
        ),
    )
    expected_at = period_start + timedelta(days=1)
    actual_at = expected_at + timedelta(days=1)
    tickets_service.ticket_sla_events.create(
        db_session,
        TicketSlaEventCreate(
            ticket_id=ticket.id,
            event_type="response",
            expected_at=expected_at,
            actual_at=actual_at,
        ),
    )
    report = sla_credit_service.sla_credit_reports.create(
        db_session,
        SlaCreditReportCreate(period_start=period_start, period_end=period_end),
    )
    assert report.items
    item = report.items[0]
    sla_credit_service.sla_credit_items.update(
        db_session, str(item.id), SlaCreditItemUpdate(approved=True)
    )
    result = sla_credit_service.sla_credit_reports.apply(
        db_session, str(report.id), SlaCreditApplyRequest()
    )
    assert result.items_applied == 1
    refreshed_invoice = billing_service.invoices.get(db_session, str(invoice.id))
    assert refreshed_invoice.balance_due < Decimal("100.00")
    credit_note = billing_service.credit_notes.list(
        db_session,
        account_id=str(subscriber_account.id),
        invoice_id=str(invoice.id),
        status=None,
        is_active=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )[0]
    assert credit_note.status in {CreditNoteStatus.partially_applied, CreditNoteStatus.applied}
