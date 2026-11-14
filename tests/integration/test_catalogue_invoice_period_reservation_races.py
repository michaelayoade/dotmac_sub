"""Real migrated PostgreSQL arbitration of catalogue and automatic drafts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.billing import Invoice, InvoiceLine, InvoiceStatus
from app.models.catalog import (
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Reseller, Subscriber
from app.schemas.billing import (
    InvoiceCreate,
    InvoiceLineCreate,
    SystemInvoiceLineCreate,
)
from app.services.billing.invoices import (
    DraftInvoiceLineReplacement,
    InvoiceLines,
    InvoicePeriodReservationError,
    Invoices,
)


@pytest.mark.parametrize("second_is_automatic", [False, True])
def test_two_writers_create_one_service_period_claim(
    engine: Engine, second_is_automatic: bool
) -> None:
    """Two callers with different source keys cannot both claim an interval."""
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    start = datetime(2030, 1, 2, tzinfo=UTC)
    end = datetime(2030, 2, 2, tzinfo=UTC)
    with sessions() as setup:
        reseller = Reseller(name=f"Claim Race {suffix}", code=f"claim-race-{suffix}")
        account = Subscriber(
            first_name="Catalogue",
            last_name="Race",
            email=f"claim-race-{suffix}@example.com",
            reseller=reseller,
        )
        offer = CatalogOffer(
            name=f"Claim Race {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            unit_price=Decimal("100"),
            start_at=start,
            next_billing_at=start,
        )
        setup.add(subscription)
        setup.commit()
        account_id, subscription_id = account.id, subscription.id

    barrier = Barrier(2)

    def save(automatic: bool) -> bool:
        with sessions() as worker:
            barrier.wait(timeout=15)
            try:
                header = InvoiceCreate(
                    account_id=account_id,
                    invoice_number=f"CLAIM-{suffix}-{'AUTO' if automatic else uuid4().hex[:8]}",
                    currency="NGN",
                )
                metadata: dict[str, object] = {
                    "kind": "base_subscription",
                    "billing_period_start": start.isoformat(),
                    "billing_period_end": end.isoformat(),
                }
                if automatic:
                    invoice = Invoices.stage_system_invoice(
                        worker, header, reason="scheduled_billing_run"
                    )
                    InvoiceLines.stage_system_line(
                        worker,
                        SystemInvoiceLineCreate(
                            invoice_id=invoice.id,
                            subscription_id=subscription_id,
                            description="Scheduled monthly service",
                            quantity=Decimal("1"),
                            unit_price=Decimal("100"),
                            billing_line_key=f"scheduled:{suffix}",
                            metadata_=metadata,
                        ),
                        reason="scheduled_base_subscription",
                    )
                else:
                    metadata.update(
                        future_catalogue_invoice=True,
                        future_invoice={"period_kind": "monthly"},
                    )
                    invoice = Invoices.stage_admin_draft(worker, header)
                    InvoiceLines.replace_admin_draft_lines(
                        worker,
                        invoice.id,
                        (
                            DraftInvoiceLineReplacement(
                                payload=InvoiceLineCreate(
                                    invoice_id=invoice.id,
                                    subscription_id=subscription_id,
                                    description="Catalogue monthly service",
                                    quantity=Decimal("1"),
                                    unit_price=Decimal("100"),
                                    metadata_=metadata,
                                )
                            ),
                        ),
                    )
                worker.commit()
                return True
            except InvoicePeriodReservationError as exc:
                worker.rollback()
                assert exc.code == "financial.invoice.period_reserved"
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [False, second_is_automatic]))
    assert sorted(results) == [False, True]
    with sessions() as inspect:
        invoices = tuple(
            inspect.scalars(
                select(Invoice).where(Invoice.account_id == account_id)
            ).all()
        )
        lines = tuple(
            inspect.scalars(
                select(InvoiceLine).where(
                    InvoiceLine.subscription_id == subscription_id
                )
            ).all()
        )
        assert len(invoices) == len(lines) == 1
        assert lines[0].invoice_id == invoices[0].id
        assert invoices[0].status is InvoiceStatus.draft
