"""The seven-dimension parity report is read-only and honest about its limits.

Two things are being proved here, and the second matters more than the first:

1. the report writes nothing — it is evidence, not a repair;
2. "cannot be compared" is reported as its own outcome with a pinned reason,
   never folded into `matched` or `diverged`. A parity claim that silently
   counts an unmeasurable position as agreement covers less than it appears to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceLine, InvoiceStatus
from app.models.billing_receivable_projection import BillingReceivableProjection
from app.models.catalog import BillingCycle
from app.models.subscription_billing_treatment import (
    BillingTreatmentReason,
    BillingTreatmentStatus,
    SubscriptionBillingArrangement,
    SubscriptionBillingTreatment,
)
from app.services.billing.receivable_cohort import (
    NotExpressibleReason,
    ParityDimension,
    ParityOutcome,
    ReceivableCohortWindow,
)
from app.services.billing.receivable_parity import evaluate_receivable_parity
from app.services.billing.receivable_projection import (
    ProjectionMode,
    ReconcileReceivableProjectionCommand,
    reconcile_receivable_projection,
)
from app.services.owner_commands import CommandContext

WINDOW_START = datetime(2026, 7, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 1, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 2, tzinfo=UTC)
ISSUED = datetime(2026, 7, 10, tzinfo=UTC)


def _window() -> ReceivableCohortWindow:
    return ReceivableCohortWindow(
        cutoff_at=CUTOFF, window_start=WINDOW_START, window_end=WINDOW_END
    )


def _context() -> CommandContext:
    return CommandContext.system(
        actor="pytest:receivable-parity",
        scope="receivable-parity",
        reason="pytest parity",
        idempotency_key=f"pytest:{uuid4()}",
    )


def _seed_invoice(db, *, subscriber, subscription, due_offset_days: int = 14):
    invoice = Invoice(
        account_id=subscriber.id,
        status=InvoiceStatus.issued,
        currency="NGN",
        subtotal=Decimal("25000.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("25000.00"),
        balance_due=Decimal("25000.00"),
        billing_period_start=WINDOW_START,
        billing_period_end=WINDOW_END,
        issued_at=ISSUED,
        due_at=ISSUED + timedelta(days=due_offset_days),
        due_date_basis=InvoiceDueDateBasis.contract_terms,
        due_date_basis_ref=f"subscription:{subscription.id}",
        due_date_policy_version="billing-payment-terms-v1",
        created_at=datetime(2026, 7, 5, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id,
            description="Standard Internet",
            quantity=Decimal("1.000"),
            unit_price=Decimal("25000.00"),
            amount=Decimal("25000.00"),
            is_active=True,
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        )
    )
    db.flush()
    return invoice


def _project(db) -> None:
    reconcile_receivable_projection(
        db,
        ReconcileReceivableProjectionCommand(
            context=_context(),
            window=_window(),
            code_version="pytest-code",
            database_schema_version="558_receivable_observation_projection",
            mode=ProjectionMode.APPLY,
        ),
    )


def _report(db):
    return evaluate_receivable_parity(
        db,
        window=_window(),
        context=_context(),
        code_version="pytest-code",
        database_schema_version="558_receivable_observation_projection",
    )


def _verdict(report, key: str, dimension: ParityDimension):
    position = next(item for item in report.positions if item.receivable_key == key)
    return next(item for item in position.verdicts if item.dimension is dimension)


def test_the_report_writes_nothing(db_session, subscriber, subscription):
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)
    before = db_session.execute(
        select(func.count()).select_from(BillingReceivableProjection)
    ).scalar_one()

    report = _report(db_session)

    after = db_session.execute(
        select(func.count()).select_from(BillingReceivableProjection)
    ).scalar_one()
    assert before == after
    assert report.evaluated_count == 1


def test_every_declared_dimension_is_reported(db_session, subscriber, subscription):
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    report = _report(db_session)

    assert set(report.by_dimension) == {item.value for item in ParityDimension}
    for dimension, outcomes in report.by_dimension.items():
        assert set(outcomes) == {item.value for item in ParityOutcome}, dimension
        assert sum(outcomes.values()) == report.evaluated_count, dimension


def test_the_three_outcome_counts_account_for_every_verdict(
    db_session, subscriber, subscription
):
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    report = _report(db_session)

    total = report.matched_count + report.diverged_count + report.not_expressible_count
    assert total == report.evaluated_count * len(ParityDimension)


def test_settlement_parity_reads_the_incumbent_resolver(
    db_session, subscriber, subscription
):
    """Settlement is compared against `resolve_invoice_settlement_amounts`.

    Re-summing allocations here would make the report a competing derivation of
    the very number it is checking.
    """
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    report = _report(db_session)
    key = report.positions[0].receivable_key
    verdict = _verdict(report, key, ParityDimension.SETTLEMENTS)

    assert verdict.outcome is ParityOutcome.MATCHED
    assert "resolver" in verdict.detail


def test_receivable_amount_parity_is_stated_as_an_observation(
    db_session, subscriber, subscription
):
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    report = _report(db_session)
    key = report.positions[0].receivable_key
    verdict = _verdict(report, key, ParityDimension.RECEIVABLE_AMOUNT)

    assert verdict.outcome is ParityOutcome.MATCHED
    assert "not a competing derivation" in verdict.detail


def test_a_complimentary_subscription_blocks_unadopted_contract_parity(
    db_session, subscriber, subscription
):
    """The a3 contract is composed but has no admitted runtime parity mapping.

    The correct answer is a counted refusal carrying the pin coordinates —
    never `matched` because nothing contradicted it, and never a locally
    invented mapping before the runtime contract is admitted.
    """
    db_session.add(
        SubscriptionBillingArrangement(
            subscription_id=subscription.id,
            account_id=subscriber.id,
            authorized_offer_id=subscription.offer_id,
            treatment=SubscriptionBillingTreatment.complimentary,
            reason_code=BillingTreatmentReason.internal_service,
            reason="Internal service approved for parity coverage",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
            ends_at=datetime(2026, 9, 1, tzinfo=UTC),
            maximum_recurring_amount=Decimal("50000.00"),
            approval_policy_max_days=180,
            billing_cycle=BillingCycle.monthly,
            currency="NGN",
            status=BillingTreatmentStatus.active,
            approved_by="pytest:receivable-parity",
            approved_at=datetime(2026, 6, 1, tzinfo=UTC),
            command_id=uuid4(),
            correlation_id=uuid4(),
            idempotency_key_sha256="0" * 64,
            command_fingerprint="1" * 64,
        )
    )
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()
    assert row.observed_billing_treatment == "complimentary"
    assert row.billing_treatment_expressible is False

    report = _report(db_session)
    verdict = _verdict(report, row.receivable_key, ParityDimension.CADENCE)

    assert verdict.outcome is ParityOutcome.NOT_EXPRESSIBLE
    assert verdict.reason is (
        NotExpressibleReason.SUBSCRIPTION_BILLING_TREATMENT_NOT_ADOPTED
    )
    assert (
        report.not_expressible_reasons[
            NotExpressibleReason.SUBSCRIPTION_BILLING_TREATMENT_NOT_ADOPTED.value
        ]
        == 1
    )
    assert report.blockers[0]["pinned_package"] == "dotmac-subscriptions"
    assert report.blockers[0]["pinned_version"]


def test_a_position_with_no_obligation_is_not_expressible_not_diverged(
    db_session, subscriber, subscription
):
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    report = _report(db_session)
    key = report.positions[0].receivable_key
    verdict = _verdict(report, key, ParityDimension.OBLIGATIONS)

    assert verdict.outcome is ParityOutcome.NOT_EXPRESSIBLE
    assert verdict.reason is NotExpressibleReason.NO_SHADOW_OBLIGATION_IN_WINDOW


def test_an_unverified_due_date_basis_blocks_the_due_date_dimension(
    db_session, subscriber, subscription
):
    invoice = _seed_invoice(
        db_session, subscriber=subscriber, subscription=subscription
    )
    invoice.due_date_basis = InvoiceDueDateBasis.unknown_unverified
    invoice.due_date_basis_ref = None
    invoice.due_date_policy_version = None
    db_session.commit()
    _project(db_session)

    report = _report(db_session)
    key = report.positions[0].receivable_key
    verdict = _verdict(report, key, ParityDimension.DUE_DATE_PROVENANCE)

    assert verdict.outcome is ParityOutcome.NOT_EXPRESSIBLE
    assert verdict.reason is NotExpressibleReason.UNVERIFIED_DUE_DATE_PROVENANCE


def test_service_scope_divergence_is_reported_when_the_subscription_moves(
    db_session, subscriber, subscription
):
    """A projected scope that no longer matches the live subscription is drift."""
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    subscription.service_description = "changed"
    subscription.end_at = datetime(2027, 1, 1, tzinfo=UTC)
    db_session.commit()

    report = _report(db_session)
    key = report.positions[0].receivable_key
    verdict = _verdict(report, key, ParityDimension.SERVICE_SCOPE)

    assert verdict.outcome is ParityOutcome.DIVERGED
    assert report.diverged_count >= 1


def test_an_unprojected_member_is_counted_separately_from_parity(
    db_session, subscriber, subscription
):
    """Not projected is not the same fact as compared and agreed."""
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()

    report = _report(db_session)

    assert report.evaluated_count == 0
    assert report.unprojected_count == 1
    assert report.matched_count == 0


def test_the_run_evidence_is_typed_not_a_loose_mapping(
    db_session, subscriber, subscription
):
    _seed_invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    _project(db_session)

    evidence = _report(db_session).as_run_evidence()

    assert isinstance(evidence.matched_count, int)
    assert isinstance(evidence.not_expressible_count, int)
    assert "dimensions" in evidence.by_dimension
