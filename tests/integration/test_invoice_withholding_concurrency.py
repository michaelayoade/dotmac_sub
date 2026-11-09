"""Migrated schema refuses fabricated native projection and legacy cash adoption.

Native discharge/allocation concurrency belongs to Starter's real PostgreSQL
withholding tests. These Sub tests exercise its projection/adoption boundary.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.billing import Invoice, InvoiceStatus, Payment
from app.models.subscriber import Reseller, Subscriber
from app.services import invoice_withholding as owner
from app.services.owner_commands import CommandContext


def test_competing_legacy_cash_corrections_refuse_without_creating_cash(engine):
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    invoice_id, legacy_cash_id = uuid4(), uuid4()
    barrier = Barrier(2)

    def correct(index):
        with sessions() as db:
            before = db.scalar(select(func.count()).select_from(Payment))
            db.rollback()
            barrier.wait(timeout=20)
            with pytest.raises(owner.InvoiceWithholdingError) as refused:
                owner.confirm_invoice_withholding(
                    db,
                    owner.InvoiceWithholdingConfirm(
                        owner.InvoiceWithholdingPreviewRequest(
                            invoice_id, legacy_cash_id
                        ),
                        "untrusted-review",
                        f"claim:{index}",
                    ),
                    context=CommandContext.system(
                        actor="user:test-finance",
                        scope="billing:payment:create",
                        reason="Reject unadopted legacy cash",
                        idempotency_key=f"legacy-adoption:{uuid4()}",
                    ),
                )
            assert refused.value.code.endswith("native_cash_adoption_required")
            assert db.scalar(select(func.count()).select_from(Payment)) == before

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(correct, (1, 2)))


def test_sql_cannot_mint_native_withholding_projection(engine):
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid4().hex
    with sessions() as db:
        reseller = Reseller(name=f"Projection guard {suffix}", code=f"pg-{suffix}")
        account = Subscriber(
            first_name="Projection",
            last_name="Guard",
            email=f"native-wht-{suffix}@example.com",
            reseller=reseller,
        )
        db.add(account)
        db.flush()
        invoice = Invoice(
            account_id=account.id,
            invoice_number=f"NATIVE-GUARD-{suffix}",
            status=InvoiceStatus.issued,
            withholding_tax_selected=True,
            withholding_tax_selection_version=1,
            currency="NGN",
            subtotal=Decimal("100"),
            total=Decimal("100"),
            balance_due=Decimal("100"),
        )
        db.add(invoice)
        db.commit()
        invoice_id = invoice.id
    with sessions() as bypass:
        with pytest.raises(DBAPIError, match="requires its owner"):
            bypass.execute(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(
                    starter_withholding_discharge_id=uuid4(),
                    starter_withholding_tax_set_id=uuid4(),
                )
            )
        bypass.rollback()
        invoice = bypass.get(Invoice, invoice_id)
        assert invoice.starter_withholding_discharge_id is None
        assert invoice.starter_withholding_tax_set_id is None
