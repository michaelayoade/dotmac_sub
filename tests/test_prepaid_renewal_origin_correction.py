"""Finance-reviewed correction of a malformed prepaid-renewal adjustment reference.

An unreversed ``prepaid_service_renewal`` debit whose ``origin_ref`` is not the
canonical ``<subscription>:<start>:<end>`` quarantines the account
(``malformed_renewal_origin``). When Finance decides the debit is legitimate,
this owner rewrites only the reference, proven by structured entitlement
evidence, and never moves money.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.admin_alert import AdminAlert
from app.models.audit import AuditEvent
from app.models.billing import (
    AccountAdjustment,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    LedgerCategory,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    ServiceEntitlement,
    ServiceEntitlementStatus,
)
from app.models.catalog import BillingCycle, BillingMode, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.subscriber import SubscriberStatus
from app.models.system_user import SystemUser
from app.services.collections import scheduled
from app.services.collections.scheduled import repair_prepaid_coverage_evidence
from app.services.owner_commands import CommandContext
from app.services.prepaid_coverage_quarantine_review import (
    PrepaidCoverageQuarantineReviewQuery,
    ResolutionRoute,
    review_prepaid_coverage_quarantine,
)
from app.services.prepaid_coverage_reconciliation import (
    CoverageReconciliationReason,
    parse_prepaid_renewal_origin_ref,
    resolve_prepaid_coverage_enforcement_blockers,
)
from app.services.prepaid_renewal_origin_correction import (
    CORRECTION_PERMISSION,
    QUARANTINE_FINDING_PREFIX,
    CorrectRenewalOriginCommand,
    EntitlementAction,
    RenewalOriginBlocker,
    RenewalOriginCorrectionError,
    RenewalOriginCorrectionQuery,
    RenewalOriginDisposition,
    RenewalOriginWarning,
    canonical_origin_ref,
    correct_renewal_origin,
    preview_renewal_origin_correction,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
START = datetime(2026, 7, 22, 7, 33, 48, 976393, tzinfo=UTC)
END = datetime(2026, 8, 20, 7, 33, 48, 976393, tzinfo=UTC)
PRICE = Decimal("17500.00")
DEBIT = Decimal("18812.50")
SHA = "c" * 64
BOTH_WARNINGS = (
    RenewalOriginWarning.entitlement_amount_differs_from_debit,
    RenewalOriginWarning.entitlement_invoice_backed,
)


def _staff(db, name: str = "Finance") -> SystemUser:
    user = SystemUser(
        id=uuid4(),
        first_name=name,
        last_name="Operator",
        display_name=f"{name} Operator",
        email=f"{name.lower()}-{uuid4().hex}@example.test",
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def _prepare(db, account, subscription) -> None:
    account.billing_mode = BillingMode.prepaid
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    subscription.billing_cycle = BillingCycle.monthly
    subscription.unit_price = PRICE
    subscription.next_billing_at = NOW - timedelta(days=30)
    db.commit()


def _debit(
    db,
    account,
    *,
    origin_ref: str | None,
    amount: Decimal = DEBIT,
    ledger_amount: Decimal | None = None,
    origin: str = "prepaid_service_renewal",
) -> tuple[AccountAdjustment, LedgerEntry]:
    ledger = LedgerEntry(
        account_id=account.id,
        entry_type=LedgerEntryType.debit,
        source=LedgerSource.adjustment,
        category=LedgerCategory.internet_service,
        amount=ledger_amount if ledger_amount is not None else amount,
        currency="NGN",
        memo="Prepaid service renewal",
        effective_date=START,
        created_at=START,
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
        origin=origin,
        origin_ref=origin_ref,
        prepaid_funding_before=Decimal("20000.00"),
        prepaid_funding_after=Decimal("20000.00") - amount,
        postpaid_receivables=Decimal("0.00"),
        collection_blocking_balance=Decimal("0.00"),
        access_consequence="none_adjustment_only",
        preview_fingerprint="a" * 64,
        idempotency_key=f"pytest-origin-{ledger.id}",
        ledger_entry_id=ledger.id,
        created_at=START,
    )
    db.add(adjustment)
    db.commit()
    return adjustment, ledger


def _paid_invoice(db, account, subscription) -> tuple[Invoice, InvoiceLine]:
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-ORIGIN-{uuid4().hex[:8]}",
        status=InvoiceStatus.paid,
        currency="NGN",
        subtotal=PRICE,
        total=PRICE,
        balance_due=Decimal("0.00"),
        billing_period_start=START,
        billing_period_end=END,
        issued_at=START,
        paid_at=START,
    )
    db.add(invoice)
    db.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        subscription_id=subscription.id,
        description="Unlimited Basic",
        quantity=Decimal("1.000"),
        unit_price=PRICE,
        amount=PRICE,
        metadata_={"kind": "base_subscription"},
    )
    db.add(line)
    db.commit()
    return invoice, line


def _entitlement(
    db,
    account,
    subscription,
    *,
    ledger: LedgerEntry | None = None,
    invoice: Invoice | None = None,
    line: InvoiceLine | None = None,
    amount: Decimal = PRICE,
    start: datetime = START,
    end: datetime = END,
    status: ServiceEntitlementStatus = ServiceEntitlementStatus.active,
) -> ServiceEntitlement:
    row = ServiceEntitlement(
        account_id=account.id,
        subscription_id=subscription.id,
        source_ledger_entry_id=ledger.id if ledger is not None else None,
        source_invoice_id=invoice.id if invoice is not None else None,
        source_invoice_line_id=line.id if line is not None else None,
        starts_at=start,
        ends_at=end,
        amount_funded=amount,
        currency="NGN",
        status=status,
    )
    db.add(row)
    db.commit()
    return row


def _one_cycle_end() -> datetime:
    from app.models.catalog import BillingCycle
    from app.services.catalog.subscriptions import billing_cycle_end

    return billing_cycle_end(START, BillingCycle.monthly)


def _linked(query_adjustment, entitlement, **overrides) -> RenewalOriginCorrectionQuery:
    values = {
        "adjustment_id": query_adjustment.id,
        "disposition": RenewalOriginDisposition.entitlement_already_linked,
        "entitlement_id": entitlement.id,
        "acknowledged_warnings": BOTH_WARNINGS,
    }
    values.update(overrides)
    return RenewalOriginCorrectionQuery(**values)


def _context(user: SystemUser, key: str, *, scope: str = CORRECTION_PERMISSION):
    return CommandContext.system(
        actor=f"user:{user.id}",
        scope=scope,
        reason="finance-reviewed renewal origin correction test",
        idempotency_key=key,
    )


def _confirm(
    db,
    query,
    user,
    *,
    fingerprint=None,
    key=None,
    granted=True,
    scope=CORRECTION_PERMISSION,
):
    if fingerprint is None:
        fingerprint = preview_renewal_origin_correction(
            db, query, as_of=NOW
        ).fingerprint
    user_id = user.id
    context = _context(user, key or f"confirm-{uuid4()}", scope=scope)
    db.commit()  # adapters hand owners a transaction-free session
    return correct_renewal_origin(
        db,
        CorrectRenewalOriginCommand(
            query=query,
            preview_fingerprint=fingerprint,
            reason="Finance confirmed service was delivered; reference only is wrong",
            evidence_reference="finance-ticket-FIN-77",
            evidence_sha256=SHA,
            corrected_by=user_id,
            permission_granted=granted,
        ),
        context=context,
    )


def _open_work_item(db) -> None:
    repair_prepaid_coverage_evidence(db, now=NOW)
    db.commit()


def _work_item(db, account) -> AdminAlert:
    return (
        db.query(AdminAlert)
        .filter(AdminAlert.fingerprint == f"{QUARANTINE_FINDING_PREFIX}{account.id}")
        .one()
    )


def _case_a(db, account, subscription):
    """Bare invoice id as origin_ref; one invoice-backed ledger-linked entitlement."""
    _prepare(db, account, subscription)
    invoice, line = _paid_invoice(db, account, subscription)
    adjustment, ledger = _debit(db, account, origin_ref=str(invoice.id))
    entitlement = _entitlement(
        db, account, subscription, ledger=ledger, invoice=invoice, line=line
    )
    return adjustment, ledger, entitlement, invoice


def test_prefix_matches_the_scheduled_work_item_contract():
    assert QUARANTINE_FINDING_PREFIX == (
        scheduled.PREPAID_COVERAGE_QUARANTINE_FINDING_PREFIX
    )


def test_canonical_reference_is_exactly_what_the_reconciliation_parser_accepts():
    sub = uuid4()
    ref = canonical_origin_ref(sub, START, END)

    assert (
        ref
        == f"{sub}:2026-07-22T07:33:48.976393+00:00:2026-08-20T07:33:48.976393+00:00"
    )
    assert parse_prepaid_renewal_origin_ref(ref) == (sub, START, END)


def test_preview_proves_the_reference_from_the_linked_entitlement(
    db_session, subscriber_account, subscription
):
    adjustment, ledger, entitlement, _invoice = _case_a(
        db_session, subscriber_account, subscription
    )
    _open_work_item(db_session)

    preview = preview_renewal_origin_correction(
        db_session, _linked(adjustment, entitlement), as_of=NOW
    )

    assert preview.actionable, preview.blockers
    assert preview.origin_ref_before == str(_invoice.id)
    assert preview.origin_ref_after == canonical_origin_ref(subscription.id, START, END)
    assert preview.planned.action is EntitlementAction.none
    assert preview.warnings == tuple(
        sorted(BOTH_WARNINGS, key=lambda value: value.value)
    )
    assert preview.adjustment.amount == DEBIT
    assert preview.entitlement is not None
    assert preview.entitlement.amount_funded == PRICE
    effect = preview.quarantine_effect
    assert effect.work_item_open is True
    assert effect.current_blocking_reasons == (
        CoverageReconciliationReason.malformed_renewal_origin,
    )
    assert effect.malformed_adjustment_ids_before == (adjustment.id,)
    assert effect.malformed_adjustment_ids_after == ()
    assert effect.corrected_period_is_current is False
    assert effect.projected_blocking_reasons == ()
    assert effect.work_item_resolves_on_next_sweep is True
    # Read-only and deterministic.
    again = preview_renewal_origin_correction(
        db_session, _linked(adjustment, entitlement), as_of=NOW
    )
    assert again.fingerprint == preview.fingerprint
    db_session.refresh(adjustment)
    assert adjustment.origin_ref == str(_invoice.id)


def test_unacknowledged_and_surplus_warnings_are_blockers(
    db_session, subscriber_account, subscription
):
    adjustment, _ledger, entitlement, _invoice = _case_a(
        db_session, subscriber_account, subscription
    )

    none_ack = preview_renewal_origin_correction(
        db_session,
        _linked(adjustment, entitlement, acknowledged_warnings=()),
        as_of=NOW,
    )
    # Once the funded amount equals the debit that warning is no longer present,
    # so acknowledging it is surplus.
    entitlement.amount_funded = DEBIT
    db_session.commit()
    surplus = preview_renewal_origin_correction(
        db_session, _linked(adjustment, entitlement), as_of=NOW
    )

    assert RenewalOriginBlocker.unacknowledged_warning in none_ack.blockers
    assert RenewalOriginBlocker.acknowledged_warning_not_present in surplus.blockers


def test_confirm_rewrites_only_the_reference_and_clears_the_quarantine(
    db_session, subscriber_account, subscription
):
    adjustment, ledger, entitlement, invoice = _case_a(
        db_session, subscriber_account, subscription
    )
    _open_work_item(db_session)
    assert _work_item(db_session, subscriber_account).status.value != "resolved"
    assert (
        resolve_prepaid_coverage_enforcement_blockers(
            db_session, [subscription], as_of=NOW
        )[0].reason
        is CoverageReconciliationReason.malformed_renewal_origin
    )
    staff = _staff(db_session)
    query = _linked(adjustment, entitlement)
    adjustment_id, ledger_id, entitlement_id = adjustment.id, ledger.id, entitlement.id

    result = _confirm(db_session, query, staff, key="origin-a-1")

    assert result.replayed is False
    assert result.entitlement_action is EntitlementAction.none
    assert result.entitlement_id == entitlement_id
    assert result.projected_blocking_reasons == ()
    db_session.expire_all()
    adjustment = db_session.get(AccountAdjustment, adjustment_id)
    assert adjustment.origin_ref == canonical_origin_ref(subscription.id, START, END)
    # Money, the ledger debit, and the entitlement are untouched.
    assert adjustment.amount == DEBIT
    assert adjustment.reversed_at is None
    ledger = db_session.get(LedgerEntry, ledger_id)
    assert (ledger.amount, ledger.is_active) == (DEBIT, True)
    assert db_session.query(LedgerEntry).count() == 1
    entitlement = db_session.get(ServiceEntitlement, entitlement_id)
    assert (entitlement.starts_at.replace(tzinfo=UTC), entitlement.amount_funded) == (
        START,
        PRICE,
    )
    assert db_session.query(ServiceEntitlement).count() == 1
    assert db_session.get(Invoice, invoice.id).total == PRICE
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "correct_prepaid_renewal_origin_ref")
        .one()
    )
    assert audit.entity_id == str(adjustment_id)
    assert audit.actor_id == str(staff.id)
    assert audit.metadata_["economic_delta"] == "0.00"
    assert audit.metadata_["origin_ref_before"] == str(invoice.id)
    event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "prepaid_renewal_origin.corrected")
        .one()
    )
    assert event.payload["adjustment_id"] == str(adjustment_id)

    # Every path that computes malformed_renewal_origin now agrees.
    assert (
        resolve_prepaid_coverage_enforcement_blockers(
            db_session, [subscription], as_of=NOW
        )
        == ()
    )
    review = review_prepaid_coverage_quarantine(
        db_session,
        PrepaidCoverageQuarantineReviewQuery(
            account_ids=(subscriber_account.id,), as_of=NOW
        ),
    )
    assert review.accounts[0].renewal_origin_findings == ()
    _open_work_item(db_session)
    assert _work_item(db_session, subscriber_account).status.value == "resolved"


def test_replay_returns_the_stored_outcome_and_a_new_key_is_refused(
    db_session, subscriber_account, subscription
):
    adjustment, _ledger, entitlement, _invoice = _case_a(
        db_session, subscriber_account, subscription
    )
    staff = _staff(db_session)
    query = _linked(adjustment, entitlement)
    fingerprint = preview_renewal_origin_correction(
        db_session, query, as_of=NOW
    ).fingerprint
    first = _confirm(db_session, query, staff, fingerprint=fingerprint, key="same")

    replay = _confirm(db_session, query, staff, fingerprint=fingerprint, key="same")

    assert replay.replayed is True
    assert replay.correction_id == first.correction_id
    assert replay.origin_ref_after == first.origin_ref_after
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "correct_prepaid_renewal_origin_ref")
        .count()
        == 1
    )
    other = _staff(db_session, "Other")
    with pytest.raises(RenewalOriginCorrectionError) as conflict:
        _confirm(db_session, query, other, fingerprint=fingerprint, key="same")
    assert conflict.value.code.endswith("idempotency_conflict")
    # Once canonical, a fresh key has nothing to correct.
    after = preview_renewal_origin_correction(db_session, query, as_of=NOW)
    assert RenewalOriginBlocker.origin_ref_already_canonical in after.blockers
    with pytest.raises(RenewalOriginCorrectionError) as refused:
        _confirm(db_session, query, staff, fingerprint=after.fingerprint, key="new")
    assert refused.value.code.endswith("not_actionable")


def test_confirm_requires_permission_scope_key_reason_and_evidence(
    db_session, subscriber_account, subscription
):
    adjustment, _ledger, entitlement, _invoice = _case_a(
        db_session, subscriber_account, subscription
    )
    staff = _staff(db_session)
    query = _linked(adjustment, entitlement)

    with pytest.raises(RenewalOriginCorrectionError) as denied:
        _confirm(db_session, query, staff, granted=False)
    assert denied.value.code.endswith("permission_denied")
    with pytest.raises(RenewalOriginCorrectionError) as wrong_scope:
        _confirm(db_session, query, staff, scope="billing:ledger:write")
    assert wrong_scope.value.code.endswith("permission_denied")
    with pytest.raises(RenewalOriginCorrectionError) as stale:
        _confirm(db_session, query, staff, fingerprint="0" * 64)
    assert stale.value.code.endswith("stale_preview")
    inactive = _staff(db_session, "Gone")
    inactive.is_active = False
    db_session.commit()
    with pytest.raises(RenewalOriginCorrectionError) as invalid_actor:
        _confirm(db_session, query, inactive)
    assert invalid_actor.value.code.endswith("invalid_actor")

    staff_id = staff.id
    fingerprint = preview_renewal_origin_correction(
        db_session, query, as_of=NOW
    ).fingerprint
    for reason, reference, digest, expected in (
        ("too short", "FIN-1", SHA, "invalid_reason"),
        ("x" * 20, "", SHA, "invalid_evidence"),
        ("x" * 20, "FIN-1", "z" * 64, "invalid_evidence"),
    ):
        db_session.commit()
        with pytest.raises(RenewalOriginCorrectionError) as bad:
            correct_renewal_origin(
                db_session,
                CorrectRenewalOriginCommand(
                    query=query,
                    preview_fingerprint=fingerprint,
                    reason=reason,
                    evidence_reference=reference,
                    evidence_sha256=digest,
                    corrected_by=staff_id,
                    permission_granted=True,
                ),
                context=CommandContext.system(
                    actor=f"user:{staff_id}",
                    scope=CORRECTION_PERMISSION,
                    reason="invalid evidence probe",
                    idempotency_key=f"bad-{expected}-{uuid4()}",
                ),
            )
        assert bad.value.code.endswith(expected)
    db_session.commit()
    with pytest.raises(RenewalOriginCorrectionError) as no_key:
        correct_renewal_origin(
            db_session,
            CorrectRenewalOriginCommand(
                query=query,
                preview_fingerprint=fingerprint,
                reason="x" * 20,
                evidence_reference="FIN-1",
                evidence_sha256=SHA,
                corrected_by=staff_id,
                permission_granted=True,
            ),
            context=CommandContext.system(
                actor=f"user:{staff_id}",
                scope=CORRECTION_PERMISSION,
                reason="missing key",
            ),
        )
    assert no_key.value.code.endswith("missing_idempotency_key")
    db_session.expire_all()
    assert db_session.get(AccountAdjustment, adjustment.id).origin_ref == str(
        _invoice.id
    )


def test_link_existing_entitlement_records_the_debit_as_its_funding_source(
    db_session, subscriber_account, subscription
):
    """Case B shape: the debit funded an invoice-backed entitlement, unlinked."""
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(db_session, subscriber_account, subscription)
    adjustment, ledger = _debit(
        db_session,
        subscriber_account,
        origin_ref="invoice:INV-109440:replace-legacy-installation-funding:2026-09-27",
        amount=Decimal("18812.00"),
    )
    entitlement = _entitlement(
        db_session, subscriber_account, subscription, invoice=invoice, line=line
    )
    staff = _staff(db_session)
    query = RenewalOriginCorrectionQuery(
        adjustment_id=adjustment.id,
        disposition=RenewalOriginDisposition.link_existing_entitlement,
        entitlement_id=entitlement.id,
        acknowledged_warnings=BOTH_WARNINGS,
    )
    preview = preview_renewal_origin_correction(db_session, query, as_of=NOW)
    assert preview.actionable, preview.blockers
    assert preview.planned.action is EntitlementAction.link_debit_to_existing
    assert preview.origin_ref_after == canonical_origin_ref(subscription.id, START, END)
    ledger_id, entitlement_id, invoice_id = ledger.id, entitlement.id, invoice.id

    result = _confirm(db_session, query, staff, key="origin-b-1")

    assert result.entitlement_action is EntitlementAction.link_debit_to_existing
    assert result.entitlement_id == entitlement_id
    db_session.expire_all()
    entitlement = db_session.get(ServiceEntitlement, entitlement_id)
    assert entitlement.source_ledger_entry_id == ledger_id
    assert entitlement.source_invoice_id == invoice_id
    assert entitlement.metadata_["source_account_adjustment_id"] == str(adjustment.id)
    assert entitlement.metadata_["funding_debit_link_evidence_ref"].startswith(
        "financial.prepaid_renewal_origin_correction:"
    )
    assert (entitlement.starts_at.replace(tzinfo=UTC), entitlement.amount_funded) == (
        START,
        PRICE,
    )
    assert db_session.query(ServiceEntitlement).count() == 1
    assert db_session.query(LedgerEntry).count() == 1
    assert db_session.get(AccountAdjustment, adjustment.id).origin_ref == (
        canonical_origin_ref(subscription.id, START, END)
    )
    assert (
        resolve_prepaid_coverage_enforcement_blockers(
            db_session, [subscription], as_of=NOW
        )
        == ()
    )


def test_create_entitlement_uses_the_existing_wallet_debit_writer(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, ledger = _debit(
        db_session, subscriber_account, origin_ref="renewal for July"
    )
    staff = _staff(db_session)
    end = _one_cycle_end()
    query = RenewalOriginCorrectionQuery(
        adjustment_id=adjustment.id,
        disposition=RenewalOriginDisposition.create_entitlement_from_debit,
        subscription_id=subscription.id,
        period_start=START,
        period_end=end,
    )
    preview = preview_renewal_origin_correction(db_session, query, as_of=NOW)
    assert preview.actionable, preview.blockers
    assert preview.position_impact.invoices_made_documentary == ()
    assert preview.position_impact.coverage_end_before is None
    assert preview.planned.action is EntitlementAction.create_from_debit
    assert preview.planned.amount_funded == DEBIT
    ledger_id = ledger.id

    result = _confirm(db_session, query, staff, key="origin-create-1")

    assert result.entitlement_action is EntitlementAction.create_from_debit
    db_session.expire_all()
    (entitlement,) = db_session.query(ServiceEntitlement).all()
    assert entitlement.id == result.entitlement_id
    assert entitlement.source_ledger_entry_id == ledger_id
    assert entitlement.amount_funded == DEBIT
    assert entitlement.status is ServiceEntitlementStatus.active
    assert db_session.query(LedgerEntry).count() == 1
    assert db_session.get(AccountAdjustment, adjustment.id).origin_ref == (
        canonical_origin_ref(subscription.id, START, end)
    )


def test_create_blocks_on_unnamed_overlap_and_unknown_acknowledgement(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, _ledger = _debit(
        db_session, subscriber_account, origin_ref="renewal for July"
    )
    other = _entitlement(db_session, subscriber_account, subscription)
    end = START + timedelta(days=30)

    def query(**overrides):
        values = {
            "adjustment_id": adjustment.id,
            "disposition": RenewalOriginDisposition.create_entitlement_from_debit,
            "subscription_id": subscription.id,
            "period_start": START,
            "period_end": end,
        }
        values.update(overrides)
        return RenewalOriginCorrectionQuery(**values)

    unnamed = preview_renewal_origin_correction(db_session, query(), as_of=NOW)
    foreign = preview_renewal_origin_correction(
        db_session,
        query(acknowledged_overlapping_entitlement_ids=(uuid4(),)),
        as_of=NOW,
    )
    named = preview_renewal_origin_correction(
        db_session,
        query(acknowledged_overlapping_entitlement_ids=(other.id,)),
        as_of=NOW,
    )

    assert RenewalOriginBlocker.overlapping_entitlement_unresolved in unnamed.blockers
    assert RenewalOriginBlocker.acknowledged_overlap_not_found in foreign.blockers
    assert RenewalOriginBlocker.overlapping_entitlement_unresolved not in named.blockers
    assert [row.entitlement_id for row in named.overlapping_entitlements] == [other.id]


@pytest.mark.parametrize(
    "build, blocker",
    [
        ("reversed", RenewalOriginBlocker.adjustment_reversed),
        ("not_renewal", RenewalOriginBlocker.adjustment_not_renewal_debit),
        ("ledger_mismatch", RenewalOriginBlocker.ledger_evidence_inconsistent),
        ("canonical_already", RenewalOriginBlocker.origin_ref_already_canonical),
        ("inactive_entitlement", RenewalOriginBlocker.entitlement_not_active),
        ("other_debit", RenewalOriginBlocker.entitlement_not_linked_to_debit),
    ],
)
def test_preview_blocks_unsafe_evidence(
    db_session, subscriber_account, subscription, build, blocker
):
    _prepare(db_session, subscriber_account, subscription)
    origin_ref = "renewal for July"
    kwargs = {}
    if build == "not_renewal":
        kwargs["origin"] = "manual"
    if build == "ledger_mismatch":
        kwargs["ledger_amount"] = Decimal("18000.00")
    if build == "canonical_already":
        origin_ref = canonical_origin_ref(subscription.id, START, END)
    adjustment, ledger = _debit(
        db_session, subscriber_account, origin_ref=origin_ref, **kwargs
    )
    if build == "reversed":
        adjustment.reversed_at = NOW
        db_session.commit()
    other_adjustment, other_ledger = _debit(
        db_session, subscriber_account, origin_ref=origin_ref
    )
    entitlement = _entitlement(
        db_session,
        subscriber_account,
        subscription,
        ledger=other_ledger if build == "other_debit" else ledger,
        status=(
            ServiceEntitlementStatus.reversed
            if build == "inactive_entitlement"
            else ServiceEntitlementStatus.active
        ),
    )

    preview = preview_renewal_origin_correction(
        db_session,
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.entitlement_already_linked,
            entitlement_id=entitlement.id,
            acknowledged_warnings=(
                RenewalOriginWarning.entitlement_amount_differs_from_debit,
            ),
        ),
        as_of=NOW,
    )

    assert blocker in preview.blockers
    assert preview.actionable is False
    assert other_adjustment.id != adjustment.id


def test_multiple_linked_entitlements_and_duplicate_canonical_reference_block(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, ledger = _debit(
        db_session, subscriber_account, origin_ref="renewal for July"
    )
    first = _entitlement(db_session, subscriber_account, subscription, ledger=ledger)
    # The active-ledger-entry unique index forbids a second active link, so a
    # competing claim is another adjustment already carrying the canonical ref.
    _debit(
        db_session,
        subscriber_account,
        origin_ref=canonical_origin_ref(subscription.id, START, END),
    )

    preview = preview_renewal_origin_correction(
        db_session,
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.entitlement_already_linked,
            entitlement_id=first.id,
            acknowledged_warnings=(
                RenewalOriginWarning.entitlement_amount_differs_from_debit,
            ),
        ),
        as_of=NOW,
    )

    assert (
        RenewalOriginBlocker.canonical_origin_used_by_other_adjustment
        in preview.blockers
    )


def test_another_malformed_adjustment_keeps_the_projection_quarantined(
    db_session, subscriber_account, subscription
):
    adjustment, _ledger, entitlement, _invoice = _case_a(
        db_session, subscriber_account, subscription
    )
    other, _other_ledger = _debit(
        db_session, subscriber_account, origin_ref="still malformed"
    )

    preview = preview_renewal_origin_correction(
        db_session, _linked(adjustment, entitlement), as_of=NOW
    )

    assert preview.actionable, preview.blockers
    effect = preview.quarantine_effect
    assert set(effect.malformed_adjustment_ids_before) == {adjustment.id, other.id}
    assert effect.malformed_adjustment_ids_after == (other.id,)
    assert effect.projected_blocking_reasons == (
        CoverageReconciliationReason.malformed_renewal_origin,
    )
    assert effect.work_item_resolves_on_next_sweep is False


def test_query_shape_is_validated(db_session, subscriber_account, subscription):
    adjustment, _ledger = _debit(db_session, subscriber_account, origin_ref="bad")
    for bad in (
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.entitlement_already_linked,
        ),
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.link_existing_entitlement,
            entitlement_id=uuid4(),
            subscription_id=subscription.id,
        ),
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.create_entitlement_from_debit,
            subscription_id=subscription.id,
        ),
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.create_entitlement_from_debit,
            subscription_id=subscription.id,
            period_start=START.replace(tzinfo=None),
            period_end=END,
        ),
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.create_entitlement_from_debit,
            subscription_id=subscription.id,
            period_start=END,
            period_end=START,
        ),
    ):
        with pytest.raises(RenewalOriginCorrectionError):
            preview_renewal_origin_correction(db_session, bad, as_of=NOW)


def test_review_routes_a_legitimate_debit_to_the_reviewed_correction(
    db_session, subscriber_account, subscription
):
    adjustment, _ledger, entitlement, _invoice = _case_a(
        db_session, subscriber_account, subscription
    )

    (finding,) = (
        review_prepaid_coverage_quarantine(
            db_session,
            PrepaidCoverageQuarantineReviewQuery(
                account_ids=(subscriber_account.id,), as_of=NOW
            ),
        )
        .accounts[0]
        .renewal_origin_findings
    )

    (option,) = finding.options
    assert option.route is ResolutionRoute.reviewed_renewal_origin_correction
    assert option.sanctioned is True
    assert option.owner == "financial.prepaid_renewal_origin_correction"
    assert str(adjustment.id) in (option.command or "")
    assert f"--entitlement-id {entitlement.id}" in (option.command or "")
    assert "entitlement_already_linked" in (option.command or "")
    # The prefilled read-only preview really is accepted by the owner.
    preview = preview_renewal_origin_correction(
        db_session,
        RenewalOriginCorrectionQuery(
            adjustment_id=adjustment.id,
            disposition=RenewalOriginDisposition.entitlement_already_linked,
            entitlement_id=entitlement.id,
            acknowledged_warnings=BOTH_WARNINGS,
        ),
        as_of=NOW,
    )
    assert preview.actionable, preview.blockers


def _settle(db, account, invoice: Invoice) -> None:
    payment = Payment(
        account_id=account.id,
        amount=invoice.total,
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=START,
        is_active=True,
    )
    db.add(payment)
    db.flush()
    db.add(
        PaymentAllocation(
            payment_id=payment.id, invoice_id=invoice.id, amount=invoice.total
        )
    )
    db.commit()


def _link_query(adjustment, entitlement, **overrides):
    values = {
        "adjustment_id": adjustment.id,
        "disposition": RenewalOriginDisposition.link_existing_entitlement,
        "entitlement_id": entitlement.id,
        "acknowledged_warnings": (),
    }
    values.update(overrides)
    return RenewalOriginCorrectionQuery(**values)


def test_link_blocks_when_the_entitlement_invoice_is_already_settled(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(db_session, subscriber_account, subscription)
    adjustment, _ledger = _debit(
        db_session, subscriber_account, origin_ref="renewal", amount=Decimal("18812.00")
    )
    entitlement = _entitlement(
        db_session, subscriber_account, subscription, invoice=invoice, line=line
    )
    query = _link_query(adjustment, entitlement, acknowledged_warnings=BOTH_WARNINGS)

    unsettled = preview_renewal_origin_correction(db_session, query, as_of=NOW)
    _settle(db_session, subscriber_account, invoice)
    settled = preview_renewal_origin_correction(db_session, query, as_of=NOW)

    assert unsettled.actionable, unsettled.blockers
    assert RenewalOriginBlocker.entitlement_invoice_already_settled in settled.blockers
    assert settled.actionable is False
    assert settled.fingerprint != unsettled.fingerprint


def test_link_blocks_when_it_would_make_a_paid_invoice_documentary(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, _line = _paid_invoice(db_session, subscriber_account, subscription)
    _settle(db_session, subscriber_account, invoice)
    # Debit equals the invoice total and the entitlement has the invoice's
    # exact period, but is not invoice-backed: linking would drop the invoice's
    # customer-position consumption.
    adjustment, _ledger = _debit(
        db_session, subscriber_account, origin_ref="renewal", amount=PRICE
    )
    entitlement = _entitlement(db_session, subscriber_account, subscription)
    query = _link_query(adjustment, entitlement)

    before = preview_renewal_origin_correction(db_session, query, as_of=NOW)

    assert RenewalOriginBlocker.would_make_invoice_documentary in before.blockers
    assert before.position_impact.invoices_made_documentary == (invoice.id,)
    assert before.actionable is False
    if before.position_impact.prepaid_available_balance_before is not None:
        assert (
            before.position_impact.prepaid_available_balance_after
            == before.position_impact.prepaid_available_balance_before + PRICE
        )
    # A different amount does not touch the documentary set.
    other, _ = _debit(
        db_session, subscriber_account, origin_ref="renewal two", amount=DEBIT
    )
    other_entitlement = _entitlement(
        db_session,
        subscriber_account,
        subscription,
        start=START + timedelta(days=60),
        end=END + timedelta(days=60),
    )
    clear = preview_renewal_origin_correction(
        db_session, _link_query(other, other_entitlement), as_of=NOW
    )
    assert clear.position_impact.invoices_made_documentary == ()
    assert RenewalOriginBlocker.would_make_invoice_documentary not in clear.blockers


def _create_query(adjustment, subscription, **overrides):
    values = {
        "adjustment_id": adjustment.id,
        "disposition": RenewalOriginDisposition.create_entitlement_from_debit,
        "subscription_id": subscription.id,
        "period_start": START,
        "period_end": _one_cycle_end(),
    }
    values.update(overrides)
    return RenewalOriginCorrectionQuery(**values)


def test_create_blocks_a_period_that_is_not_exactly_one_cycle(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, _ledger = _debit(db_session, subscriber_account, origin_ref="renewal")

    short = preview_renewal_origin_correction(
        db_session,
        _create_query(adjustment, subscription, period_end=START + timedelta(days=20)),
        as_of=NOW,
    )
    long = preview_renewal_origin_correction(
        db_session,
        _create_query(adjustment, subscription, period_end=START + timedelta(days=45)),
        as_of=NOW,
    )

    assert RenewalOriginBlocker.period_not_one_billing_cycle in short.blockers
    assert RenewalOriginBlocker.period_not_one_billing_cycle in long.blockers
    assert RenewalOriginBlocker.period_exceeds_one_billing_cycle in long.blockers
    assert RenewalOriginBlocker.period_exceeds_one_billing_cycle not in short.blockers
    assert short.actionable is False
    assert long.actionable is False


def test_create_blocks_a_period_far_from_the_debit_date(
    db_session, subscriber_account, subscription
):

    _prepare(db_session, subscriber_account, subscription)
    adjustment, _ledger = _debit(db_session, subscriber_account, origin_ref="renewal")
    far_start = START + timedelta(days=120)

    preview = preview_renewal_origin_correction(
        db_session,
        _create_query(
            adjustment,
            subscription,
            period_start=far_start,
            period_end=_monthly_end(far_start),
        ),
        as_of=NOW,
    )

    assert RenewalOriginBlocker.period_start_outside_debit_cycle in preview.blockers
    assert preview.actionable is False


def _monthly_end(start: datetime) -> datetime:
    from app.models.catalog import BillingCycle
    from app.services.catalog.subscriptions import billing_cycle_end

    return billing_cycle_end(start, BillingCycle.monthly)


def test_create_blocks_when_an_invoice_already_covers_the_cycle(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    invoice, line = _paid_invoice(db_session, subscriber_account, subscription)
    adjustment, _ledger = _debit(
        db_session, subscriber_account, origin_ref="renewal", amount=DEBIT
    )

    paid_invoice_only = preview_renewal_origin_correction(
        db_session, _create_query(adjustment, subscription), as_of=NOW
    )
    backed = _entitlement(
        db_session, subscriber_account, subscription, invoice=invoice, line=line
    )
    acknowledged = preview_renewal_origin_correction(
        db_session,
        _create_query(
            adjustment,
            subscription,
            acknowledged_overlapping_entitlement_ids=(backed.id,),
        ),
        as_of=NOW,
    )

    assert (
        RenewalOriginBlocker.cycle_already_covered_by_invoice
        in paid_invoice_only.blockers
    )
    assert (
        RenewalOriginBlocker.cycle_already_covered_by_invoice in acknowledged.blockers
    )
    assert acknowledged.actionable is False


def test_create_preview_shows_coverage_end_before_and_after(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    adjustment, _ledger = _debit(db_session, subscriber_account, origin_ref="renewal")
    inside = START + timedelta(days=3)

    preview = preview_renewal_origin_correction(
        db_session, _create_query(adjustment, subscription), as_of=inside
    )

    assert preview.actionable, preview.blockers
    assert preview.position_impact.coverage_end_before is None
    assert preview.position_impact.coverage_end_after == _one_cycle_end()
