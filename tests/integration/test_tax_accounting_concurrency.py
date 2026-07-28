"""PostgreSQL row-lock coverage for the WHT lifecycle owner."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.event_store import EventStore
from app.models.payment_proof import (
    WithholdingTaxRecord,
    WithholdingTaxStatus,
    WithholdingTaxTransition,
)
from app.models.subscriber import Reseller
from app.services import billing as billing_service
from app.services import tax_accounting
from app.services.owner_commands import CommandContext


def test_concurrent_wht_certification_rejects_conflicting_evidence(engine):
    """The record lock admits one certificate and rejects the conflicting replay."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"WHT Concurrency {suffix}",
            code=f"wht-concurrency-{suffix}",
            contact_email=f"wht-concurrency-{suffix}@example.com",
            is_active=True,
        )
        setup.add(reseller)
        setup.commit()
        account = billing_service.billing_accounts.get_for_reseller(
            setup, str(reseller.id)
        )
        record = WithholdingTaxRecord(
            billing_account_id=account.id,
            reseller_id=reseller.id,
            gross_amount=Decimal("100000.00"),
            net_amount=Decimal("95000.00"),
            wht_amount=Decimal("5000.00"),
            wht_rate=Decimal("5.00"),
            currency="NGN",
            status=WithholdingTaxStatus.pending,
        )
        setup.add(record)
        setup.commit()
        record_id = record.id

    barrier = Barrier(2)

    def certify(certificate_reference: str) -> tuple[str, str]:
        with session_factory() as worker:
            barrier.wait(timeout=10)
            try:
                result = tax_accounting.transition_withholding_tax(
                    worker,
                    tax_accounting.TransitionWithholdingTaxCommand(
                        record_id=record_id,
                        target_status=WithholdingTaxStatus.certified,
                        certificate_reference=certificate_reference,
                    ),
                    context=CommandContext.system(
                        actor="pytest:postgres-tax-accounting",
                        scope=f"withholding_tax:{record_id}",
                        reason="PostgreSQL WHT concurrency verification",
                        idempotency_key=(
                            f"wht-certification:{record_id}:{certificate_reference}"
                        ),
                    ),
                )
            except tax_accounting.TaxAccountingError as exc:
                return "error", exc.code
            return "success", result.certificate_reference or ""

    references = (f"WHT-CERT-A-{suffix}", f"WHT-CERT-B-{suffix}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(certify, references))

    assert sorted(result[0] for result in results) == ["error", "success"]
    assert [value for outcome, value in results if outcome == "error"] == [
        "financial.tax_accounting.replay_conflict"
    ]

    with session_factory() as check:
        persisted = check.get(WithholdingTaxRecord, record_id)
        assert persisted is not None
        assert persisted.status is WithholdingTaxStatus.certified
        assert persisted.certificate_reference in references
        transitions = (
            check.query(WithholdingTaxTransition)
            .filter(WithholdingTaxTransition.record_id == record_id)
            .order_by(WithholdingTaxTransition.occurred_at)
            .all()
        )
        assert [(row.from_status, row.to_status) for row in transitions] == [
            (None, WithholdingTaxStatus.pending),
            (WithholdingTaxStatus.pending, WithholdingTaxStatus.certified),
        ]
        assert (
            check.query(EventStore)
            .filter(EventStore.event_type == "withholding_tax.status_changed")
            .filter(EventStore.payload["aggregate_id"].astext == str(record_id))
            .count()
            == 1
        )
