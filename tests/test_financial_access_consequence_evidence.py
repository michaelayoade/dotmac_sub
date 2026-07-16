"""SOT coverage for dunning and financial access consequences."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.audit import AuditEvent
from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import (
    AccessCredential,
    BillingMode,
    DunningAction,
    RadiusProfile,
    SubscriptionStatus,
)
from app.models.collections import (
    DunningActionLog,
    DunningCase,
    DunningCaseStatus,
    FinancialAccessAction,
    FinancialAccessConsequence,
    FinancialAccessConsequenceEvidence,
    FinancialAccessEvidenceOperation,
    FinancialAccessOrigin,
)
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.services.account_lifecycle import suspend_subscription
from app.services.collections._core import (
    _create_action_log,
    confirm_financial_access_consequence,
    confirm_financial_access_restoration,
    preview_financial_access_consequence,
    preview_financial_access_restoration,
)


def _prepare_postpaid(db, account, subscription) -> Invoice:
    account.billing_mode = BillingMode.postpaid
    account.splynx_customer_id = None
    subscription.billing_mode = BillingMode.postpaid
    subscription.status = SubscriptionStatus.active
    invoice = Invoice(
        account_id=account.id,
        invoice_number=f"INV-ACCESS-{uuid.uuid4().hex[:8]}",
        status=InvoiceStatus.overdue,
        subtotal=Decimal("100.00"),
        tax_total=Decimal("0.00"),
        total=Decimal("100.00"),
        balance_due=Decimal("100.00"),
        due_at=datetime.now(UTC) - timedelta(days=7),
        currency="NGN",
        metadata_={},
    )
    db.add(invoice)
    db.commit()
    return invoice


def test_suspend_confirmation_links_exact_lock_and_dunning_action(
    db_session, subscriber_account, subscription
):
    invoice = _prepare_postpaid(db_session, subscriber_account, subscription)
    case = DunningCase(
        account_id=subscriber_account.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    db_session.add(case)
    db_session.commit()

    preview = preview_financial_access_consequence(
        db_session,
        str(subscriber_account.id),
        action=FinancialAccessAction.suspend,
        reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        dunning_case_id=case.id,
        overdue_days=7,
    )
    assert preview.eligible is True
    assert preview.decision_inputs["overdue_receivables"] == [
        {
            "invoice_id": str(invoice.id),
            "currency": "NGN",
            "receivable": "100.00",
            "payments_applied": "0.00",
            "credits_applied": "0.00",
        }
    ]

    result = confirm_financial_access_consequence(
        db_session,
        str(subscriber_account.id),
        action=FinancialAccessAction.suspend,
        reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"test-dunning-suspend-{case.id}",
        source=f"dunning_case:{case.id}",
        dunning_case_id=case.id,
        overdue_days=7,
    )
    log = _create_action_log(
        db_session,
        case,
        DunningAction.suspend,
        7,
        str(invoice.id),
        outcome=result.consequence.outcome,
        access_consequence=result.consequence,
    )
    db_session.flush()

    evidence = db_session.query(FinancialAccessConsequenceEvidence).one()
    lock = db_session.get(EnforcementLock, evidence.enforcement_lock_id)
    assert result.consequence.outcome == "suspended"
    assert evidence.operation == FinancialAccessEvidenceOperation.lock_created
    assert lock is not None and lock.is_active is True
    assert lock.reason == EnforcementReason.overdue
    assert lock.access_mode == AccessRestrictionMode.hard_reject
    assert result.consequence.access_mode == AccessRestrictionMode.hard_reject
    assert log.access_consequence_id == result.consequence.id
    assert (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "confirm_financial_access_consequence")
        .count()
        == 1
    )

    replay = confirm_financial_access_consequence(
        db_session,
        str(subscriber_account.id),
        action=FinancialAccessAction.suspend,
        reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"test-dunning-suspend-{case.id}",
        source=f"dunning_case:{case.id}",
        dunning_case_id=case.id,
        overdue_days=7,
    )
    assert replay.idempotent_replay is True
    assert replay.consequence.id == result.consequence.id
    assert db_session.query(EnforcementLock).count() == 1
    assert db_session.query(FinancialAccessConsequence).count() == 1
    assert db_session.query(DunningActionLog).count() == 1


def test_suspend_confirmation_rejects_stale_preview(
    db_session, subscriber_account, subscription
):
    invoice = _prepare_postpaid(db_session, subscriber_account, subscription)
    preview = preview_financial_access_consequence(
        db_session,
        str(subscriber_account.id),
        action=FinancialAccessAction.suspend,
        reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        overdue_days=7,
    )
    invoice.metadata_ = {"reconciliation_hold": True}
    db_session.commit()

    with pytest.raises(HTTPException, match="changed after preview"):
        confirm_financial_access_consequence(
            db_session,
            str(subscriber_account.id),
            action=FinancialAccessAction.suspend,
            reason=EnforcementReason.overdue,
            origin=FinancialAccessOrigin.dunning,
            preview_fingerprint=preview.fingerprint,
            idempotency_key=f"stale-access-{subscriber_account.id}",
            source="test:stale",
            overdue_days=7,
        )

    assert db_session.query(EnforcementLock).count() == 0
    assert db_session.query(FinancialAccessConsequence).count() == 0


def test_restore_confirmation_links_lock_case_and_exact_profile(
    db_session, subscriber_account, subscription, monkeypatch
):
    invoice = _prepare_postpaid(db_session, subscriber_account, subscription)
    case = DunningCase(
        account_id=subscriber_account.id,
        status=DunningCaseStatus.open,
        started_at=datetime.now(UTC),
    )
    full_profile = RadiusProfile(name="Full speed", is_active=True)
    throttle_profile = RadiusProfile(name="Collections throttle", is_active=True)
    db_session.add_all([case, full_profile, throttle_profile])
    db_session.flush()
    credential = AccessCredential(
        subscriber_id=subscriber_account.id,
        subscription_id=subscription.id,
        username=f"access-{uuid.uuid4().hex[:8]}",
        is_active=True,
        radius_profile_id=throttle_profile.id,
        pre_throttle_radius_profile_id=full_profile.id,
    )
    db_session.add(credential)
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source=f"dunning_case:{case.id}",
    )
    invoice.status = InvoiceStatus.paid
    invoice.balance_due = Decimal("0.00")
    db_session.commit()

    def _setting(_db, _domain, key):
        return str(throttle_profile.id) if key == "throttle_radius_profile_id" else None

    monkeypatch.setattr(
        "app.services.collections._core.settings_spec.resolve_value", _setting
    )
    preview = preview_financial_access_restoration(
        db_session, str(subscriber_account.id)
    )
    result = confirm_financial_access_restoration(
        db_session,
        str(subscriber_account.id),
        preview_fingerprint=preview.fingerprint,
        idempotency_key=f"test-access-restore-{subscriber_account.id}",
        resolved_by="test:payment",
    )
    db_session.flush()
    db_session.refresh(subscription)
    db_session.refresh(case)
    db_session.refresh(credential)

    operations = {
        evidence.operation
        for evidence in db_session.query(FinancialAccessConsequenceEvidence).all()
    }
    assert result.consequence.outcome == "restored"
    assert result.subscriptions_changed == 1
    assert operations == {
        FinancialAccessEvidenceOperation.lock_resolved,
        FinancialAccessEvidenceOperation.credential_restored,
        FinancialAccessEvidenceOperation.dunning_case_resolved,
    }
    assert subscription.status == SubscriptionStatus.active
    assert case.status == DunningCaseStatus.resolved
    assert credential.radius_profile_id == full_profile.id
    assert credential.pre_throttle_radius_profile_id is None


def test_restore_does_not_guess_legacy_pre_throttle_profile(
    db_session, subscriber_account, subscription, monkeypatch
):
    invoice = _prepare_postpaid(db_session, subscriber_account, subscription)
    invoice.status = InvoiceStatus.paid
    invoice.balance_due = Decimal("0.00")
    throttle_profile = RadiusProfile(name="Legacy throttle", is_active=True)
    db_session.add(throttle_profile)
    db_session.flush()
    credential = AccessCredential(
        subscriber_id=subscriber_account.id,
        subscription_id=subscription.id,
        username=f"legacy-{uuid.uuid4().hex[:8]}",
        is_active=True,
        radius_profile_id=throttle_profile.id,
        pre_throttle_radius_profile_id=None,
    )
    db_session.add(credential)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.collections._core.settings_spec.resolve_value",
        lambda _db, _domain, key: (
            str(throttle_profile.id) if key == "throttle_radius_profile_id" else None
        ),
    )
    preview = preview_financial_access_restoration(
        db_session, str(subscriber_account.id)
    )

    assert preview.credential_changes == ()
    assert preview.decision_inputs["legacy_throttle_credential_ids"] == [
        str(credential.id)
    ]
    assert preview.outcome == "no_change"
