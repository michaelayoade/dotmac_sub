"""Behaviour coverage for the `billing.receivable_projection` owner.

Fast lane: in-memory SQLite. It proves classification, convergence and the
planner's staleness decision. It deliberately does NOT claim the structural
guarantees — the sequence and the BEFORE UPDATE trigger are PostgreSQL objects
installed by migration `558`, and their coverage lives in
`tests/integration/test_receivable_projection_monotonic.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing import Invoice, InvoiceDueDateBasis, InvoiceLine, InvoiceStatus
from app.models.billing_receivable_projection import (
    BillingReceivableProjection,
    ReceivableProjectionRun,
    ReceivableProjectionRunKind,
)
from app.models.catalog import BillingMode
from app.services.billing.receivable_cohort import (
    CohortClassification,
    CohortWindowError,
    ReceivableCohortWindow,
    ReceivableLane,
    definition_payload,
    definition_seal,
    membership_digest,
    receivable_key,
)
from app.services.billing.receivable_projection import (
    ApplyOutcome,
    ProjectionMode,
    ReceivableProjectionError,
    ReconcileReceivableProjectionCommand,
    plan_receivable_projection,
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


def _context(key: str | None = None) -> CommandContext:
    return CommandContext.system(
        actor="pytest:receivable-projection",
        scope="receivable-projection",
        reason="pytest receivable projection",
        idempotency_key=key or f"pytest:{uuid4()}",
    )


def _command(**overrides) -> ReconcileReceivableProjectionCommand:
    base = {
        "context": _context(),
        "window": _window(),
        "code_version": "pytest-code",
        "database_schema_version": "558_receivable_observation_projection",
    }
    base.update(overrides)
    return ReconcileReceivableProjectionCommand(**base)


def _invoice(
    db,
    *,
    subscriber,
    subscription,
    status: InvoiceStatus = InvoiceStatus.issued,
    total: str = "25000.00",
    balance_due: str = "25000.00",
    linked: bool = True,
    issued_at: datetime | None = ISSUED,
) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=status,
        currency="NGN",
        subtotal=Decimal(total),
        tax_total=Decimal("0.00"),
        total=Decimal(total),
        balance_due=Decimal(balance_due),
        billing_period_start=datetime(2026, 7, 1, tzinfo=UTC),
        billing_period_end=datetime(2026, 8, 1, tzinfo=UTC),
        issued_at=issued_at,
        due_at=(issued_at + timedelta(days=14)) if issued_at else None,
        due_date_basis=(
            InvoiceDueDateBasis.contract_terms
            if issued_at
            else InvoiceDueDateBasis.unknown_unverified
        ),
        due_date_basis_ref=f"subscription:{subscription.id}" if issued_at else None,
        due_date_policy_version="billing-payment-terms-v1" if issued_at else None,
        created_at=datetime(2026, 7, 5, tzinfo=UTC),
        updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        is_active=True,
    )
    db.add(invoice)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=invoice.id,
            subscription_id=subscription.id if linked else None,
            description="Standard Internet",
            quantity=Decimal("1.000"),
            unit_price=Decimal(total),
            amount=Decimal(total),
            is_active=True,
            created_at=datetime(2026, 7, 5, tzinfo=UTC),
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        )
    )
    db.flush()
    return invoice


# ── The sealed cohort ───────────────────────────────────────────────────────


def test_the_definition_seal_is_reproducible_without_a_database() -> None:
    assert definition_seal(_window()) == definition_seal(_window())


def test_the_definition_seal_moves_when_the_window_moves() -> None:
    other = ReceivableCohortWindow(
        cutoff_at=CUTOFF,
        window_start=WINDOW_START,
        window_end=WINDOW_END - timedelta(days=1),
    )
    assert definition_seal(_window()) != definition_seal(other)


def test_the_sealed_payload_names_the_rule_rather_than_only_hashing_it() -> None:
    """A seal test that compares two digests passes when both are of nothing."""
    payload = definition_payload(_window())
    assert payload["anchor"] == "invoices"
    assert "issued" in payload["declared_invoice_statuses"]
    assert "draft" in payload["excluded_invoice_statuses"]
    assert payload["standing_blockers"], "the treatment blocker must be sealed in"
    lanes = set(payload["lanes"])
    assert lanes == {
        ReceivableLane.POSTPAID_RECEIVABLE.value,
        ReceivableLane.PREPAID_CONSUMPTION.value,
    }, "the cohort must span both collection modes, not postpaid alone"


def test_the_membership_digest_ignores_order_and_duplicates() -> None:
    assert membership_digest(["b", "a"]) == membership_digest(["a", "b", "a"])


def test_a_naive_window_instant_is_refused_not_coerced() -> None:
    with pytest.raises(CohortWindowError):
        ReceivableCohortWindow(
            cutoff_at=CUTOFF,
            window_start=datetime(2026, 7, 1),
            window_end=WINDOW_END,
        )


def test_a_cutoff_before_the_window_end_is_refused() -> None:
    with pytest.raises(CohortWindowError):
        ReceivableCohortWindow(
            cutoff_at=WINDOW_START,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )


# ── Classification ──────────────────────────────────────────────────────────


def test_a_linked_issued_invoice_is_a_member(db_session, subscriber, subscription):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    plan = plan_receivable_projection(db_session, _command())

    assert len(plan.dispositions) == 1
    disposition = plan.dispositions[0]
    assert disposition.classification in (
        CohortClassification.COVERED,
        CohortClassification.NOT_EXPRESSIBLE,
    )
    assert disposition.position is not None
    assert disposition.position.lane is ReceivableLane.POSTPAID_RECEIVABLE


def test_a_prepaid_subscription_lands_in_the_prepaid_lane(
    db_session, subscriber, subscription
):
    """The cohort must not silently cover postpaid alone."""
    subscription.billing_mode = BillingMode.prepaid
    db_session.flush()
    _invoice(db_session, subscriber=subscriber, subscription=subscription)

    plan = plan_receivable_projection(db_session, _command())
    position = plan.dispositions[0].position
    assert position is not None
    assert position.lane is ReceivableLane.PREPAID_CONSUMPTION


def test_a_draft_is_a_declared_exclusion_not_a_silent_drop(
    db_session, subscriber, subscription
):
    _invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        status=InvoiceStatus.draft,
        issued_at=None,
    )
    plan = plan_receivable_projection(db_session, _command())

    assert plan.dispositions[0].classification is (
        CohortClassification.EXCLUDED_BY_STATUS
    )
    assert plan.classification_counts()["excluded_by_status"] == 1


def test_an_invoice_with_no_subscription_link_is_counted_unexpected_unlinked(
    db_session, subscriber, subscription
):
    _invoice(db_session, subscriber=subscriber, subscription=subscription, linked=False)
    plan = plan_receivable_projection(db_session, _command())
    assert plan.dispositions[0].classification is (
        CohortClassification.UNEXPECTED_UNLINKED
    )


def test_the_classification_is_exhaustive(db_session, subscriber, subscription):
    """Counts must sum to the candidate total; there is no residual bucket."""
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    _invoice(
        db_session,
        subscriber=subscriber,
        subscription=subscription,
        status=InvoiceStatus.void,
        issued_at=None,
    )
    _invoice(db_session, subscriber=subscriber, subscription=subscription, linked=False)
    plan = plan_receivable_projection(db_session, _command())
    assert sum(plan.classification_counts().values()) == len(plan.dispositions)


# ── Dry run ─────────────────────────────────────────────────────────────────


def test_a_dry_run_writes_nothing_at_all(db_session, subscriber, subscription):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()

    result = reconcile_receivable_projection(db_session, _command())

    assert result.mode is ProjectionMode.DRY_RUN
    assert result.run_id is None, "a dry run that reports a run id claims evidence"
    assert result.inserted_count == 1
    assert db_session.execute(select(BillingReceivableProjection)).first() is None
    assert db_session.execute(select(ReceivableProjectionRun)).first() is None


def test_a_dry_run_reports_the_same_counts_the_apply_would(
    db_session, subscriber, subscription
):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()

    dry = reconcile_receivable_projection(db_session, _command())
    db_session.rollback()
    applied = reconcile_receivable_projection(
        db_session, _command(mode=ProjectionMode.APPLY)
    )

    assert dry.inserted_count == applied.inserted_count
    assert dry.membership_digest == applied.membership_digest
    assert dry.cohort_definition_seal == applied.cohort_definition_seal


# ── Idempotence and the monotonic guard ─────────────────────────────────────


def test_reconciliation_is_idempotent(db_session, subscriber, subscription):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()

    first = reconcile_receivable_projection(
        db_session, _command(mode=ProjectionMode.APPLY)
    )
    second = reconcile_receivable_projection(
        db_session, _command(mode=ProjectionMode.APPLY)
    )

    assert first.inserted_count == 1
    assert second.inserted_count == 0
    assert second.unchanged_count == 1
    rows = db_session.execute(select(BillingReceivableProjection)).scalars().all()
    assert len(rows) == 1


def test_a_newer_source_advances_the_projection_version(
    db_session, subscriber, subscription
):
    invoice = _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))
    before = (
        db_session.execute(select(BillingReceivableProjection)).scalars().one()
    ).projection_version

    invoice.balance_due = Decimal("10000.00")
    invoice.status = InvoiceStatus.partially_paid
    invoice.updated_at = datetime(2026, 7, 20, tzinfo=UTC)
    db_session.commit()

    result = reconcile_receivable_projection(
        db_session, _command(mode=ProjectionMode.APPLY)
    )
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    assert result.updated_count == 1
    assert row.projection_version > before
    assert row.observed_outstanding_amount == Decimal("10000.0000")


def test_a_stale_observation_is_skipped_rather_than_written(
    db_session, subscriber, subscription
):
    """The planner refuses a rewind before any statement is issued."""
    invoice = _invoice(db_session, subscriber=subscriber, subscription=subscription)
    invoice.updated_at = datetime(2026, 7, 20, tzinfo=UTC)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))
    stored = db_session.execute(select(BillingReceivableProjection)).scalars().one()
    stored_version = stored.projection_version

    # Rewind the source: an older fact arriving after a newer one.
    invoice.balance_due = Decimal("1.00")
    invoice.updated_at = datetime(2026, 7, 6, tzinfo=UTC)
    db_session.commit()

    result = reconcile_receivable_projection(
        db_session, _command(mode=ProjectionMode.APPLY)
    )
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    assert result.stale_skipped_count == 1
    assert result.updated_count == 0
    assert row.projection_version == stored_version
    assert row.observed_outstanding_amount == Decimal("25000.0000")


def test_an_equal_watermark_with_a_different_fingerprint_fails_closed(
    db_session, subscriber, subscription
):
    """Two facts at one instant is not a tie to break by whichever ran last."""
    invoice = _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))

    # Change a projected value while leaving every watermark input untouched.
    db_session.execute(
        Invoice.__table__.update()
        .where(Invoice.__table__.c.id == invoice.id)
        .values(
            status=InvoiceStatus.overdue.value,
            updated_at=datetime(2026, 7, 5, tzinfo=UTC),
        )
    )
    db_session.commit()

    result = reconcile_receivable_projection(
        db_session, _command(mode=ProjectionMode.APPLY)
    )
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    assert result.ambiguous_watermark_count == 1
    assert result.updated_count == 0
    assert row.observed_invoice_status == InvoiceStatus.issued.value


# ── Provenance and rebuild ──────────────────────────────────────────────────


def test_the_input_fingerprint_is_reproducible_across_a_rebuild(
    db_session, subscriber, subscription
):
    """A rebuild must reproduce the source fingerprint byte for byte.

    `projection_version`, `projected_at` and `projected_by_run_id` are excluded
    from the fingerprint by design: they change on every rebuild, and folding
    them in would leave the projection unable to prove it reproduced anything.
    """
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))
    first = db_session.execute(select(BillingReceivableProjection)).scalars().one()
    original_fingerprint = first.input_row_fingerprint
    original_run = first.projected_by_run_id

    db_session.execute(BillingReceivableProjection.__table__.delete())
    db_session.commit()

    reconcile_receivable_projection(
        db_session,
        _command(
            mode=ProjectionMode.APPLY,
            run_kind=ReceivableProjectionRunKind.backfill,
        ),
    )
    rebuilt = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    assert rebuilt.input_row_fingerprint == original_fingerprint
    assert rebuilt.projected_by_run_id != original_run


def test_the_row_carries_everything_a_rebuild_needs(
    db_session, subscriber, subscription
):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))
    row = db_session.execute(select(BillingReceivableProjection)).scalars().one()

    assert row.cohort_definition_seal == definition_seal(_window())
    assert row.projection_policy_version
    assert row.invoice_id is not None
    assert row.subscription_id == subscription.id
    assert row.account_id == subscriber.id
    assert len(row.input_row_fingerprint) == 64
    assert len(row.service_scope_fingerprint) == 64
    assert row.receivable_key == receivable_key(
        invoice_id=str(row.invoice_id), lane=ReceivableLane.POSTPAID_RECEIVABLE
    )


def test_the_run_row_carries_the_cutover_evidence_fields(
    db_session, subscriber, subscription
):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))
    run = db_session.execute(select(ReceivableProjectionRun)).scalars().one()

    assert run.cohort_definition_seal == definition_seal(_window())
    assert len(run.membership_digest) == 64
    assert len(run.source_fingerprint) == 64
    assert len(run.result_fingerprint) == 64
    assert run.code_version == "pytest-code"
    assert run.database_schema_version
    assert run.unclassified_count == 0, "the classification must be exhaustive"
    assert run.blockers, "the standing treatment blocker must be recorded"
    assert run.blockers[0]["pinned_package"] == "dotmac-subscriptions"


# ── Drift ───────────────────────────────────────────────────────────────────


def test_a_member_that_leaves_its_cohort_is_reported_not_pruned(
    db_session, subscriber, subscription
):
    """Deleting a projected row on a source change destroys the audit trail.

    "The cohort changed" is not the same fact as "this observation was wrong".
    """
    invoice = _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))

    invoice.is_active = False
    db_session.commit()

    result = reconcile_receivable_projection(
        db_session,
        _command(
            mode=ProjectionMode.APPLY,
            run_kind=ReceivableProjectionRunKind.drift_repair,
        ),
    )

    assert result.orphaned_count == 1
    assert db_session.execute(select(BillingReceivableProjection)).scalars().all()


def test_a_different_window_does_not_report_the_other_windows_rows_as_drift(
    db_session, subscriber, subscription
):
    """Orphan detection is scoped to the seal, or every reconcile reports drift."""
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))

    later = ReceivableCohortWindow(
        cutoff_at=datetime(2026, 10, 1, tzinfo=UTC),
        window_start=datetime(2026, 9, 1, tzinfo=UTC),
        window_end=datetime(2026, 9, 30, tzinfo=UTC),
    )
    result = reconcile_receivable_projection(
        db_session, _command(window=later, mode=ProjectionMode.APPLY)
    )

    assert result.orphaned_count == 0
    assert result.cohort_count == 0


def test_drift_repair_restores_a_deleted_projection_row(
    db_session, subscriber, subscription
):
    _invoice(db_session, subscriber=subscriber, subscription=subscription)
    db_session.commit()
    reconcile_receivable_projection(db_session, _command(mode=ProjectionMode.APPLY))
    db_session.execute(BillingReceivableProjection.__table__.delete())
    db_session.commit()

    result = reconcile_receivable_projection(
        db_session,
        _command(
            mode=ProjectionMode.APPLY,
            run_kind=ReceivableProjectionRunKind.drift_repair,
        ),
    )

    assert result.missing_count == 1
    assert result.inserted_count == 1
    assert db_session.execute(select(BillingReceivableProjection)).scalars().one()


# ── Fail-closed command validation ──────────────────────────────────────────


def test_a_pass_without_an_idempotency_key_is_refused(db_session):
    command = _command(
        context=CommandContext.system(
            actor="pytest",
            scope="receivable-projection",
            reason="missing key",
        )
    )
    with pytest.raises(ReceivableProjectionError) as excinfo:
        reconcile_receivable_projection(db_session, command)
    assert excinfo.value.code.endswith("missing_idempotency_key")


def test_a_pass_without_a_code_version_is_refused(db_session):
    with pytest.raises(ReceivableProjectionError) as excinfo:
        reconcile_receivable_projection(db_session, _command(code_version="  "))
    assert excinfo.value.code.endswith("incomplete_run_identity")


def test_the_apply_outcome_vocabulary_is_closed() -> None:
    assert {item.value for item in ApplyOutcome} == {
        "inserted",
        "updated",
        "unchanged",
        "stale_skipped",
        "ambiguous_watermark",
    }
