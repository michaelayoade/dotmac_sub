from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.admin_alert import AdminAlert
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.subscriber import SubscriberStatus
from app.services.collections import scheduled
from app.services.collections.scheduled import repair_prepaid_coverage_evidence
from app.services.prepaid_coverage_quarantine_review import (
    QUARANTINE_FINDING_PREFIX,
    RUNBOOK,
    InvoicePeriodDefect,
    PeriodProof,
    PrepaidCoverageQuarantineReviewQuery,
    RenewalOriginDefect,
    ResolutionRoute,
    review_prepaid_coverage_quarantine,
)
from app.services.prepaid_coverage_reconciliation import (
    CoverageReconciliationDecision,
    CoverageReconciliationReason,
    resolve_prepaid_coverage_enforcement_blockers,
)

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
PAST_START = NOW - timedelta(days=90)
PAST_END = NOW - timedelta(days=60)


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = NOW + timedelta(days=30)
    db.commit()


def _paid_invoice(
    db,
    account,
    subscription,
    *,
    start: datetime | None,
    end: datetime | None,
    line_metadata: dict | None = None,
) -> tuple[Invoice, InvoiceLine]:
    invoice = Invoice(
        account_id=account.id,
        invoice_number="INV-QUARANTINE-1",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=Decimal("35000.00"),
        total=Decimal("35000.00"),
        balance_due=Decimal("0.00"),
        billing_period_start=start,
        billing_period_end=end,
        issued_at=PAST_START,
        paid_at=PAST_START,
    )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Base service",
        quantity=Decimal("1.000"),
        unit_price=Decimal("35000.00"),
        amount=Decimal("35000.00"),
        metadata_=(
            line_metadata
            if line_metadata is not None
            else {"kind": "base_subscription"}
        ),
    )
    db.add(line)
    db.commit()
    return invoice, line


def _line_entitlement(db, account, subscription, invoice, line) -> ServiceEntitlement:
    entitlement = ServiceEntitlement(
        account_id=account.id,
        subscription_id=subscription.id,
        source_invoice_id=invoice.id,
        source_invoice_line_id=line.id,
        starts_at=PAST_START,
        ends_at=PAST_END,
        amount_funded=Decimal("35000.00"),
        currency="NGN",
        status=ServiceEntitlementStatus.active,
    )
    db.add(entitlement)
    db.commit()
    return entitlement


def _renewal_adjustment(
    db,
    account,
    *,
    origin_ref: str | None,
    amount: Decimal = Decimal("18812.50"),
    ledger_amount: Decimal | None = None,
) -> tuple[AccountAdjustment, LedgerEntry]:
    ledger = LedgerEntry(
        account_id=account.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.adjustment,
        category=LedgerCategory.internet_service,
        amount=ledger_amount if ledger_amount is not None else amount,
        currency="NGN",
        memo="Prepaid service renewal",
        effective_date=PAST_START,
        created_at=PAST_START,
        is_active=True,
        affects_customer_position=True,
    )
    db.add(ledger)
    db.flush()
    adjustment = AccountAdjustment(
        account_id=account.id,
        category=LedgerCategory.internet_service,
        amount=amount,
        currency="NGN",
        memo="Prepaid service renewal",
        reason="Historical direct renewal",
        origin="prepaid_service_renewal",
        origin_ref=origin_ref,
        prepaid_funding_before=Decimal("20000.00"),
        prepaid_funding_after=Decimal("20000.00") - amount,
        postpaid_receivables=Decimal("0.00"),
        collection_blocking_balance=Decimal("0.00"),
        access_consequence="none_adjustment_only",
        preview_fingerprint="a" * 64,
        idempotency_key=f"pytest-quarantine-renewal:{ledger.id}",
        ledger_entry_id=ledger.id,
        created_at=PAST_START,
    )
    db.add(adjustment)
    db.commit()
    return adjustment, ledger


def _ledger_entitlement(db, account, subscription, ledger) -> ServiceEntitlement:
    entitlement = ServiceEntitlement(
        account_id=account.id,
        subscription_id=subscription.id,
        source_ledger_entry_id=ledger.id,
        starts_at=PAST_START,
        ends_at=PAST_END,
        amount_funded=ledger.amount,
        currency="NGN",
        status=ServiceEntitlementStatus.active,
    )
    db.add(entitlement)
    db.commit()
    return entitlement


def _review(db, account):
    return review_prepaid_coverage_quarantine(
        db,
        PrepaidCoverageQuarantineReviewQuery(account_ids=(account.id,), as_of=NOW),
    )


def _routes(options) -> dict[ResolutionRoute, bool]:
    return {option.route: option.sanctioned for option in options}


def test_prefix_and_runbook_match_the_scheduled_work_item_contract():
    assert QUARANTINE_FINDING_PREFIX == (
        scheduled.PREPAID_COVERAGE_QUARANTINE_FINDING_PREFIX
    )
    assert scheduled._QUARANTINE_FINDING_PREFIX == QUARANTINE_FINDING_PREFIX
    assert RUNBOOK == scheduled.PREPAID_COVERAGE_QUARANTINE_RUNBOOK


def test_missing_invoice_period_is_listed_with_no_structured_proof(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, start=None, end=None
    )

    review = _review(db_session, subscriber_account)

    account = review.accounts[0]
    assert account.blocking_reasons == (
        CoverageReconciliationReason.malformed_paid_invoice_period,
    )
    assert account.renewal_origin_findings == ()
    (finding,) = account.invoice_findings
    assert finding.invoice_id == invoice.id
    assert finding.invoice_number == "INV-QUARANTINE-1"
    assert finding.defect == InvoicePeriodDefect.missing_start_and_end
    assert finding.subscription_ids == (subscription.id,)
    assert finding.total == Decimal("35000.00")
    assert finding.balance_due == Decimal("0.00")
    assert [item.line_id for item in finding.lines] == [line.id]
    assert finding.lines[0].in_quarantine_scope is True
    assert finding.lines[0].line_kind == "base_subscription"
    assert finding.period_proof == PeriodProof.none
    assert finding.proven_period_start is None
    assert finding.sanctioned_repair_available is True
    assert _routes(finding.options) == {
        ResolutionRoute.reviewed_paid_invoice_period_repair: True
    }
    (option,) = finding.options
    assert option.owner == "financial.prepaid_paid_invoice_period_repair"
    assert option.missing_capability is None
    # A prefilled READ-ONLY preview; Finance supplies the documented period.
    assert option.command is not None
    assert "repair_prepaid_paid_invoice_period preview" in option.command
    assert f"--invoice-id {invoice.id}" in option.command
    assert f"--line-id {line.id}" in option.command
    assert f"--subscription-id {subscription.id}" in option.command
    assert "<finance-documented-start>" in option.command
    assert "memo" in option.when
    # The diagnostic explains exactly what enforcement blocks on.
    blockers = resolve_prepaid_coverage_enforcement_blockers(
        db_session, [subscription], as_of=NOW
    )
    assert [blocker.reason for blocker in blockers] == [
        CoverageReconciliationReason.malformed_paid_invoice_period
    ]


def test_inverted_invoice_period_reports_entitlement_proof_without_repairing(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(
        db_session, subscriber_account, subscription, start=PAST_END, end=PAST_START
    )
    entitlement = _line_entitlement(
        db_session, subscriber_account, subscription, invoice, line
    )

    (finding,) = _review(db_session, subscriber_account).accounts[0].invoice_findings

    assert finding.defect == InvoicePeriodDefect.end_not_after_start
    assert finding.period_proof == PeriodProof.source_entitlement
    assert finding.proven_period_start == PAST_START
    assert finding.proven_period_end == PAST_END
    assert finding.lines[0].source_entitlements[0].entitlement_id == entitlement.id
    assert _routes(finding.options) == {
        ResolutionRoute.reviewed_paid_invoice_period_repair: True
    }
    (option,) = finding.options
    assert option.command is not None
    assert f"--period-start {PAST_START.isoformat()}" in option.command
    assert f"--period-end {PAST_END.isoformat()}" in option.command
    db_session.refresh(invoice)
    assert invoice.billing_period_end.replace(tzinfo=UTC) == PAST_START


def test_non_service_line_and_derived_metadata_are_never_treated_as_proof(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _paid_invoice(
        db_session,
        subscriber_account,
        subscription,
        start=PAST_START,
        end=None,
        line_metadata={
            "kind": "installation",
            "billing_period_start": PAST_START.isoformat(),
            "billing_period_end": PAST_END.isoformat(),
            "billing_period_source": "paid_at_manual_invoice",
        },
    )

    (finding,) = _review(db_session, subscriber_account).accounts[0].invoice_findings

    assert finding.defect == InvoicePeriodDefect.missing_end
    assert finding.lines[0].line_kind == "installation"
    assert finding.period_proof == PeriodProof.derived_line_metadata_period
    assert _routes(finding.options) == {
        ResolutionRoute.engineering_non_service_line_classification: False,
        ResolutionRoute.reviewed_paid_invoice_period_repair: True,
    }
    repair = next(
        option
        for option in finding.options
        if option.route == ResolutionRoute.reviewed_paid_invoice_period_repair
    )
    # A derived (payment-date) period is not proof, so nothing is prefilled.
    assert repair.command is not None
    assert "<finance-documented-start>" in repair.command


def test_unparseable_renewal_origin_without_entitlement_routes_to_reviewed_reversal(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, ledger = _renewal_adjustment(
        db_session, subscriber_account, origin_ref="renewal for July"
    )

    account = _review(db_session, subscriber_account).accounts[0]

    assert account.blocking_reasons == (
        CoverageReconciliationReason.malformed_renewal_origin,
    )
    (finding,) = account.renewal_origin_findings
    assert finding.adjustment_id == adjustment.id
    assert finding.origin_ref == "renewal for July"
    assert finding.defects == (RenewalOriginDefect.origin_ref_unparseable,)
    assert finding.parsed_subscription_id is None
    assert finding.ledger.ledger_entry_id == ledger.id
    assert finding.ledger.amount == Decimal("18812.50")
    assert finding.linked_entitlements == ()
    assert _routes(finding.options) == {
        ResolutionRoute.reviewed_account_adjustment_reversal: True,
        ResolutionRoute.engineering_renewal_origin_correction: False,
    }
    reversal = next(
        option
        for option in finding.options
        if option.route == ResolutionRoute.reviewed_account_adjustment_reversal
    )
    assert str(adjustment.id) in (reversal.command or "")
    # The named owner's read-only preview accepts this debit's evidence.
    from app.schemas.billing import AccountAdjustmentReversalPreviewRequest
    from app.services.billing.adjustments import (
        PreviewAccountAdjustmentReversalQuery,
        preview_account_adjustment_reversal,
    )

    owner_preview = preview_account_adjustment_reversal(
        db_session,
        PreviewAccountAdjustmentReversalQuery(
            adjustment_id=adjustment.id,
            request=AccountAdjustmentReversalPreviewRequest(reason="FIN-123 review"),
        ),
    )
    assert owner_preview.reverses_ledger_entry_id == ledger.id


def test_missing_renewal_origin_is_reported(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _renewal_adjustment(db_session, subscriber_account, origin_ref=None)

    (finding,) = (
        _review(db_session, subscriber_account).accounts[0].renewal_origin_findings
    )

    assert finding.defects == (RenewalOriginDefect.origin_ref_missing,)


def test_non_positive_origin_with_linked_entitlement_routes_to_unused_correction(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, ledger = _renewal_adjustment(
        db_session,
        subscriber_account,
        origin_ref=(
            f"{subscription.id}:{PAST_END.isoformat()}:{PAST_START.isoformat()}"
        ),
    )
    entitlement = _ledger_entitlement(
        db_session, subscriber_account, subscription, ledger
    )

    (finding,) = (
        _review(db_session, subscriber_account).accounts[0].renewal_origin_findings
    )

    assert finding.defects == (RenewalOriginDefect.origin_period_not_positive,)
    assert finding.parsed_subscription_id == subscription.id
    assert finding.parsed_starts_at == PAST_END
    assert finding.parsed_ends_at == PAST_START
    assert [row.entitlement_id for row in finding.linked_entitlements] == [
        entitlement.id
    ]
    assert _routes(finding.options) == {
        ResolutionRoute.unused_prepaid_renewal_correction: True,
        ResolutionRoute.engineering_renewal_origin_correction: False,
    }
    unused = next(option for option in finding.options if option.sanctioned)
    assert unused.owner == "financial.prepaid_service_renewals"
    assert unused.runbook == "docs/runbooks/UNUSED_PREPAID_RENEWAL_CORRECTION.md"
    for value in (subscriber_account.id, subscription.id, adjustment.id):
        assert str(value) in (unused.command or "")
    assert f"--entitlement {entitlement.id}" in (unused.command or "")
    # The named owner really accepts this exact pair (its preview is read-only).
    from app.services.prepaid_service_renewals import (
        UnusedPrepaidRenewalCorrectionQuery,
        preview_unused_prepaid_renewal_correction,
    )

    owner_preview = preview_unused_prepaid_renewal_correction(
        db_session,
        UnusedPrepaidRenewalCorrectionQuery(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            adjustment_id=adjustment.id,
            entitlement_id=entitlement.id,
        ),
    )
    assert owner_preview.actionable is True, owner_preview.reason


def test_renewal_ledger_mismatch_has_no_sanctioned_repair(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _renewal_adjustment(
        db_session,
        subscriber_account,
        origin_ref=(
            f"{subscription.id}:{PAST_START.isoformat()}:{PAST_END.isoformat()}"
        ),
        ledger_amount=Decimal("18000.00"),
    )

    account = _review(db_session, subscriber_account).accounts[0]

    assert account.blocking_reasons == (
        CoverageReconciliationReason.malformed_renewal_origin,
    )
    (finding,) = account.renewal_origin_findings
    assert finding.defects == (RenewalOriginDefect.ledger_amount_mismatch,)
    assert finding.ledger.amount == Decimal("18000.00")
    assert _routes(finding.options) == {
        ResolutionRoute.engineering_adjustment_ledger_repair: False
    }


def test_exact_renewal_origin_is_not_a_finding(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    _renewal_adjustment(
        db_session,
        subscriber_account,
        origin_ref=(
            f"{subscription.id}:{PAST_START.isoformat()}:{PAST_END.isoformat()}"
        ),
    )

    account = _review(db_session, subscriber_account).accounts[0]

    assert account.finding_count == 0
    assert account.blocking_reasons == ()


@contextmanager
def _statements(db):
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _record)


def test_review_only_reads(db_session, subscriber_account, subscription):
    _prepare(db_session, subscriber_account, subscription)
    invoice, _line = _paid_invoice(
        db_session, subscriber_account, subscription, start=None, end=None
    )
    _renewal_adjustment(db_session, subscriber_account, origin_ref="bad")
    repair_prepaid_coverage_evidence(db_session, now=NOW)
    db_session.commit()
    counts_before = {
        model: db_session.query(model).count()
        for model in (
            AdminAlert,
            Invoice,
            InvoiceLine,
            AccountAdjustment,
            LedgerEntry,
            ServiceEntitlement,
        )
    }

    with _statements(db_session) as seen:
        review = review_prepaid_coverage_quarantine(
            db_session, PrepaidCoverageQuarantineReviewQuery(as_of=NOW)
        )

    assert review.finding_count == 2
    assert seen
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "WITH")) for statement in seen
    ), [s for s in seen if not s.lstrip().upper().startswith("SELECT")]
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted
    assert {
        model: db_session.query(model).count() for model in counts_before
    } == counts_before
    db_session.refresh(invoice)
    assert invoice.billing_period_start is None


def test_default_mode_reviews_every_open_work_item_and_links_runbook(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, _line = _paid_invoice(
        db_session, subscriber_account, subscription, start=None, end=None
    )
    repair_prepaid_coverage_evidence(db_session, now=NOW)

    alert = (
        db_session.query(AdminAlert)
        .filter(
            AdminAlert.fingerprint
            == f"{QUARANTINE_FINDING_PREFIX}{subscriber_account.id}"
        )
        .one()
    )
    assert RUNBOOK in alert.summary
    assert len(alert.summary) <= 255
    assert alert.details["runbook"] == RUNBOOK
    assert str(subscriber_account.id) in alert.details["diagnostic_command"]

    review = review_prepaid_coverage_quarantine(
        db_session, PrepaidCoverageQuarantineReviewQuery(as_of=NOW)
    )
    (account,) = review.accounts
    assert account.account_id == subscriber_account.id
    assert account.work_item is not None
    assert account.work_item.reason_codes == ("malformed_paid_invoice_period",)
    assert account.work_item.status == "open"
    assert account.subscriptions[0].decision == (
        CoverageReconciliationDecision.quarantined
    )

    # Once the source record is corrected by its owner, the next sweep closes
    # the work item and the diagnostic reports nothing left to review.
    invoice.billing_period_start = PAST_START
    invoice.billing_period_end = PAST_END
    db_session.commit()
    repair_prepaid_coverage_evidence(db_session, now=NOW)
    db_session.refresh(alert)
    assert alert.status.value == "resolved"
    review = review_prepaid_coverage_quarantine(
        db_session, PrepaidCoverageQuarantineReviewQuery(as_of=NOW)
    )
    assert review.accounts == ()


def test_cli_renders_json_and_text_without_writing(
    db_session, subscriber_account, subscription, monkeypatch, capsys
):
    import app.db as app_db
    from scripts.billing import diagnose_prepaid_coverage_quarantine as cli

    _prepare(db_session, subscriber_account, subscription)
    _renewal_adjustment(db_session, subscriber_account, origin_ref="bad")

    @contextmanager
    def _session():
        yield db_session

    monkeypatch.setattr(app_db, "read_only_snapshot_session", _session)

    code = cli.main(
        [
            "--account-id",
            str(subscriber_account.id),
            "--as-of",
            NOW.isoformat(),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["financial_state_changed"] is False
    assert payload["finding_count"] == 1
    (account,) = payload["accounts"]
    assert account["blocking_reasons"] == ["malformed_renewal_origin"]
    (finding,) = account["renewal_origin_findings"]
    assert finding["defects"] == ["origin_ref_unparseable"]
    assert finding["amount"] == "18812.50"
    assert finding["sanctioned_repair_available"] is True

    code = cli.main(["--account-id", str(subscriber_account.id)])
    text = capsys.readouterr().out
    assert code == 2
    assert "malformed_renewal_origin" in text
    assert "Read-only" in text
    assert not db_session.dirty


@pytest.mark.parametrize("value", ["not-a-uuid"])
def test_cli_rejects_invalid_account_id(value):
    from scripts.billing import diagnose_prepaid_coverage_quarantine as cli

    with pytest.raises(SystemExit):
        cli.main(["--account-id", value])
