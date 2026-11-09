"""Migrated PostgreSQL proves owner-only draft choice and issue immutability."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.billing import Invoice, InvoiceStatus
from app.models.customer_tax_policy import CustomerTaxPolicy
from app.models.subscriber import Reseller, Subscriber
from app.services import invoice_withholding as owner
from app.services.owner_commands import CommandContext


def test_selection_compare_and_swap_and_database_issue_freeze(engine, monkeypatch):
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    with sessions() as setup:
        reseller = Reseller(name=f"WHT selection {suffix}", code=f"wht-select-{suffix}")
        account = Subscriber(
            first_name="WHT",
            last_name="Selection",
            email=f"wht-select-{suffix}@example.com",
            reseller=reseller,
        )
        setup.add_all([reseller, account])
        setup.flush()
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"WHT-SELECT-{suffix}",
            status=InvoiceStatus.draft,
            withholding_tax_selected=True,
            withholding_tax_selection_version=1,
            currency="NGN",
            subtotal=Decimal("100000"),
            tax_total=Decimal("7500"),
            total=Decimal("107500"),
            balance_due=Decimal("107500"),
        )
        setup.add_all(
            [
                invoice,
                CustomerTaxPolicy(
                    account_id=account.id, withholding_tax_enabled=True, version=1
                ),
            ]
        )
        setup.commit()
        invoice_id = invoice.id
    with sessions() as bypass:
        with pytest.raises(DBAPIError, match="draft owner"):
            bypass.execute(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(
                    withholding_tax_selected=False, withholding_tax_selection_version=2
                )
            )
        bypass.rollback()
    barrier = Barrier(2)

    def choose(_):
        with sessions() as session:
            barrier.wait(timeout=20)
            try:
                result = owner.set_draft_invoice_withholding_selection(
                    session,
                    owner.SetDraftInvoiceWithholdingSelection(invoice_id, False, 1),
                    context=CommandContext.system(
                        actor="user:test-principal",
                        scope="billing:invoice:update",
                        reason="Concurrent selection qualification",
                    ),
                )
                return result.version
            except owner.InvoiceWithholdingError as exc:
                assert exc.code == "financial.invoice_withholding.stale_selection"
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(choose, range(2)))
    assert results.count(2) == 1 and results.count(None) == 1
    with sessions() as issue:
        invoice = issue.get(Invoice, invoice_id)
        assert (
            not invoice.withholding_tax_selected
            and invoice.withholding_tax_selection_version == 2
        )
        invoice.status = InvoiceStatus.issued
        issue.commit()
    with sessions() as bypass:
        with pytest.raises(DBAPIError, match="immutable"):
            bypass.execute(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(
                    withholding_tax_selected=True, withholding_tax_selection_version=3
                )
            )
        bypass.rollback()
        selected = bypass.execute(
            select(
                Invoice.withholding_tax_selected,
                Invoice.withholding_tax_selection_version,
            ).where(Invoice.id == invoice_id)
        ).one()
        assert (
            not selected.withholding_tax_selected
            and selected.withholding_tax_selection_version == 2
        )
