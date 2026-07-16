"""Prepaid enforcement cannot outrun signed materialized funding authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.subscriber import SubscriberStatus
from app.services import control_registry
from app.services.control_relationships import ControlRelationshipError
from app.services.prepaid_enforcement_readiness import (
    prepaid_enforcement_readiness_block_reason,
    record_prepaid_enforcement_readiness,
)
from tests.prepaid_funding_helpers import (
    TEST_PREPAID_POSITION_AT,
    materialize_test_prepaid_opening_balance,
)


def _prepare(db, account, subscription):
    account.status = SubscriberStatus.active
    account.is_active = True
    account.billing_enabled = True
    account.billing_mode = BillingMode.prepaid
    account.min_balance = Decimal("100.00")
    account.splynx_customer_id = None
    account.deposit = None
    subscription.status = SubscriptionStatus.active
    subscription.billing_mode = BillingMode.prepaid
    db.commit()
    materialize_test_prepaid_opening_balance(db, account.id, Decimal("0.00"))


def _set_activation(db, activation_at):
    db.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key="prepaid_enforcement_activation_at",
            value_type=SettingValueType.string,
            value_text=activation_at.isoformat(),
            is_active=True,
        )
    )
    db.commit()


def test_feature_control_rejects_enable_without_funding_readiness(db_session):
    with pytest.raises(ControlRelationshipError, match="funding readiness"):
        control_registry.update_canonical_feature_controls(
            db_session,
            payload={"collections.prepaid_balance_enforcement": True},
        )


def test_full_cohort_parity_record_allows_control_enable(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    captured_at = datetime.now(UTC)
    activation_at = captured_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)

    record = record_prepaid_enforcement_readiness(
        db_session,
        activation_at=activation_at,
        evidence_ref="reconciliation-run:prepaid-2026-07-16",
        verified_by="billing-operations",
        now=captured_at,
    )
    db_session.commit()

    assert record.candidate_account_count == 1
    assert record.blocker_count == 0
    assert len(record.reconstruction_evidence_sha256) == 64
    assert prepaid_enforcement_readiness_block_reason(db_session) is None
    changes = control_registry.update_canonical_feature_controls(
        db_session,
        payload={"collections.prepaid_balance_enforcement": True},
    )
    assert changes[0]["effective"]["to"] is True


def test_missing_materialized_authority_cannot_be_recorded_as_ready(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    captured_at = datetime.now(UTC)
    activation_at = captured_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)

    db_session.query(PrepaidFundingBaseline).delete()
    db_session.query(PrepaidFundingReconstructionBatch).delete()
    db_session.commit()

    with pytest.raises(ValueError, match="authority_cutover_missing"):
        record_prepaid_enforcement_readiness(
            db_session,
            activation_at=activation_at,
            evidence_ref="reconciliation-run:prepaid-missing-authority",
            verified_by="billing-operations",
            now=captured_at,
        )


def test_configured_activation_grace_limit_blocks_fresh_free_service(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    subscriber_account.grace_period_days = 1
    db_session.commit()
    captured_at = datetime.now(UTC)
    activation_at = captured_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)

    with pytest.raises(ValueError, match="activation_grace_exceeds_configured_max"):
        record_prepaid_enforcement_readiness(
            db_session,
            activation_at=activation_at,
            evidence_ref="reconciliation-run:prepaid-grace-check",
            verified_by="billing-operations",
            now=captured_at,
        )


def test_unactivated_readiness_expires_from_snapshot_capture(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    captured_at = datetime.now(UTC)
    activation_at = captured_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)
    record_prepaid_enforcement_readiness(
        db_session,
        activation_at=activation_at,
        evidence_ref="reconciliation-run:prepaid-expiry-check",
        verified_by="billing-operations",
        now=captured_at,
    )
    db_session.commit()

    assert (
        prepaid_enforcement_readiness_block_reason(
            db_session, now=captured_at + timedelta(minutes=61)
        )
        == "prepaid_funding_readiness_expired"
    )


def test_cutover_config_change_invalidates_unactivated_readiness(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    captured_at = datetime.now(UTC)
    activation_at = captured_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)
    record_prepaid_enforcement_readiness(
        db_session,
        activation_at=activation_at,
        evidence_ref="reconciliation-run:prepaid-config-check",
        verified_by="billing-operations",
        now=captured_at,
    )
    db_session.add(
        DomainSetting(
            domain=SettingDomain.collections,
            key="prepaid_activation_max_grace_days",
            value_type=SettingValueType.integer,
            value_text="1",
            is_active=True,
        )
    )
    db_session.commit()

    assert (
        prepaid_enforcement_readiness_block_reason(db_session)
        == "prepaid_funding_readiness_configuration_changed"
    )


def test_live_funding_change_invalidates_unactivated_readiness(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    observed_at = datetime.now(UTC)
    activation_at = observed_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)
    record_prepaid_enforcement_readiness(
        db_session,
        activation_at=activation_at,
        evidence_ref="reconciliation-run:prepaid-funding-change",
        verified_by="billing-operations",
        now=observed_at,
    )
    db_session.add(
        LedgerEntry(
            account_id=subscriber_account.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("125.00"),
            currency="NGN",
            memo="Approved post-readiness customer credit",
            effective_date=observed_at + timedelta(minutes=1),
            affects_customer_position=True,
        )
    )
    db_session.commit()

    assert (
        prepaid_enforcement_readiness_block_reason(
            db_session, now=observed_at + timedelta(minutes=1)
        )
        == "prepaid_funding_readiness_funding_changed"
    )


def test_reconstruction_supersession_invalidates_unactivated_readiness(
    db_session, subscriber_account, subscription
):
    _prepare(db_session, subscriber_account, subscription)
    observed_at = datetime.now(UTC)
    activation_at = observed_at + timedelta(minutes=10)
    _set_activation(db_session, activation_at)
    record = record_prepaid_enforcement_readiness(
        db_session,
        activation_at=activation_at,
        evidence_ref="reconciliation-run:prepaid-reconstruction-change",
        verified_by="billing-operations",
        now=observed_at,
    )
    original_hash = record.reconstruction_evidence_sha256
    db_session.commit()

    materialize_test_prepaid_opening_balance(
        db_session,
        subscriber_account.id,
        Decimal("0.00"),
        position_at=TEST_PREPAID_POSITION_AT + timedelta(days=1),
    )
    db_session.refresh(record)

    assert record.reconstruction_evidence_sha256 == original_hash
    assert (
        prepaid_enforcement_readiness_block_reason(db_session, now=observed_at)
        == "prepaid_funding_readiness_reconstruction_changed"
    )
