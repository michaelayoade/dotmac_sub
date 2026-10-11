"""Reviewed prepaid enforcement-lock recovery after wrongful expiry."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing import ServiceEntitlement, ServiceEntitlementStatus
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.subscriber import SubscriberStatus
from app.services.owner_commands import CommandContext
from app.services.prepaid_expiry_lock_repair import (
    PrepaidExpiryLockDiscoveryQuery,
    PrepaidExpiryLockRepairBlocker,
    PrepaidExpiryLockRepairCommand,
    PrepaidExpiryLockRepairError,
    PrepaidExpiryLockRepairQuery,
    apply_prepaid_expiry_lock_repair,
    discover_prepaid_expiry_lock_repairs,
    preview_prepaid_expiry_lock_repair,
)
from app.services.prepaid_service_renewals import _funding_recovery_status_clause
from scripts.billing import billing_target_shadow


def _candidate(db_session, subscriber_account, subscription):
    now = datetime.now(UTC).replace(microsecond=0)
    subscriber_account.status = SubscriberStatus.suspended
    subscriber_account.is_active = True
    subscriber_account.billing_enabled = True
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.suspended
    subscription.end_at = None
    subscription.next_billing_at = now - timedelta(days=1)
    old_lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber_account.id,
        reason=EnforcementReason.prepaid,
        source="prepaid_balance_sweep",
        is_active=False,
        resolved_at=now - timedelta(hours=1),
        resolved_by="expired",
    )
    db_session.add(old_lock)
    db_session.commit()
    return PrepaidExpiryLockRepairQuery(
        subscription_id=subscription.id,
        historical_lock_id=old_lock.id,
        as_of=now,
    )


def _command(query, fingerprint):
    return PrepaidExpiryLockRepairCommand(
        query=query,
        expected_fingerprint=fingerprint,
        context=CommandContext.system(
            actor="operator:test",
            scope=f"subscription:{query.subscription_id}",
            reason="Reviewed expiry-cleared prepaid lock",
            idempotency_key=f"repair:{query.historical_lock_id}",
        ),
    )


def test_repair_recreates_only_prepaid_lock_and_replay_is_idempotent(
    db_session, subscriber_account, subscription
):
    query = _candidate(db_session, subscriber_account, subscription)
    admin_lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscriber_account.id,
        reason=EnforcementReason.admin,
        source="admin:independent_hold",
    )
    db_session.add(admin_lock)
    db_session.commit()

    discovered = discover_prepaid_expiry_lock_repairs(
        db_session,
        PrepaidExpiryLockDiscoveryQuery(
            resolved_since=query.as_of - timedelta(hours=2),
            resolved_before=query.as_of,
            as_of=query.as_of,
        ),
    )
    assert any(item.query == query and item.repairable for item in discovered)
    assert not discover_prepaid_expiry_lock_repairs(
        db_session,
        PrepaidExpiryLockDiscoveryQuery(
            resolved_since=query.as_of - timedelta(hours=2),
            resolved_before=query.as_of,
            as_of=query.as_of,
            offset=1,
        ),
    )
    preview = preview_prepaid_expiry_lock_repair(db_session, query)
    assert preview.repairable
    db_session.commit()
    command = _command(query, preview.fingerprint)

    first = apply_prepaid_expiry_lock_repair(db_session, command)
    second = apply_prepaid_expiry_lock_repair(db_session, command)
    with pytest.raises(PrepaidExpiryLockRepairError):
        apply_prepaid_expiry_lock_repair(
            db_session,
            PrepaidExpiryLockRepairCommand(
                query=query,
                expected_fingerprint=preview.fingerprint,
                context=CommandContext.system(
                    actor="operator:other",
                    scope=f"subscription:{query.subscription_id}",
                    reason="Conflicting repair attempt",
                    idempotency_key="repair:different-key",
                ),
            ),
        )

    db_session.refresh(subscription)
    db_session.refresh(admin_lock)
    repaired = db_session.get(EnforcementLock, first.active_lock_id)
    assert first.created is True
    assert second.created is False
    assert second.active_lock_id == first.active_lock_id
    assert repaired is not None and repaired.is_active is True
    assert repaired.reason is EnforcementReason.prepaid
    assert repaired.access_mode is preview.access_mode
    assert repaired.requested_access_mode is AccessRestrictionMode.captive
    assert admin_lock.is_active is True
    assert subscription.status is SubscriptionStatus.suspended
    assert (
        db_session.scalar(
            select(Subscription.id).where(
                Subscription.id == subscription.id,
                _funding_recovery_status_clause(),
            )
        )
        == subscription.id
    )


def test_repair_refuses_changed_preview(db_session, subscriber_account, subscription):
    query = _candidate(db_session, subscriber_account, subscription)
    preview = preview_prepaid_expiry_lock_repair(db_session, query)
    assert preview.repairable
    subscription.next_billing_at = query.as_of - timedelta(days=2)
    db_session.commit()

    with pytest.raises(PrepaidExpiryLockRepairError) as exc:
        apply_prepaid_expiry_lock_repair(
            db_session, _command(query, preview.fingerprint)
        )
    assert exc.value.code.endswith("stale_preview")


def test_repair_refuses_covered_service(db_session, subscriber_account, subscription):
    query = _candidate(db_session, subscriber_account, subscription)
    db_session.add(
        ServiceEntitlement(
            account_id=subscriber_account.id,
            subscription_id=subscription.id,
            status=ServiceEntitlementStatus.active,
            starts_at=query.as_of - timedelta(days=1),
            ends_at=query.as_of + timedelta(days=1),
            amount_funded=Decimal("100.00"),
        )
    )
    db_session.commit()

    preview = preview_prepaid_expiry_lock_repair(db_session, query)

    assert not preview.repairable
    assert PrepaidExpiryLockRepairBlocker.coverage_present in preview.blockers


def test_repair_refuses_unproven_historical_lock(
    db_session, subscriber_account, subscription
):
    query = _candidate(db_session, subscriber_account, subscription)
    wrong_query = PrepaidExpiryLockRepairQuery(
        subscription_id=query.subscription_id,
        historical_lock_id=uuid4(),
        as_of=query.as_of,
    )

    preview = preview_prepaid_expiry_lock_repair(db_session, wrong_query)

    assert not preview.repairable
    assert (
        PrepaidExpiryLockRepairBlocker.historical_lock_not_expiry_prepaid
        in preview.blockers
    )


def test_repair_refuses_fixed_term_end(db_session, subscriber_account, subscription):
    query = _candidate(db_session, subscriber_account, subscription)
    subscription.end_at = query.as_of - timedelta(seconds=1)
    db_session.commit()

    preview = preview_prepaid_expiry_lock_repair(db_session, query)

    assert PrepaidExpiryLockRepairBlocker.fixed_term_ended in preview.blockers


def test_repair_refuses_stale_as_of(db_session, subscriber_account, subscription):
    query = _candidate(db_session, subscriber_account, subscription)
    stale_query = PrepaidExpiryLockRepairQuery(
        subscription_id=query.subscription_id,
        historical_lock_id=query.historical_lock_id,
        as_of=query.as_of - timedelta(minutes=16),
    )

    with pytest.raises(PrepaidExpiryLockRepairError) as exc:
        preview_prepaid_expiry_lock_repair(db_session, stale_query)
    assert exc.value.code.endswith("stale_as_of")


@pytest.mark.parametrize(
    "subcommand,extra",
    (
        ("preview-prepaid-service-renewal", ()),
        (
            "execute-reviewed-prepaid-service-renewal",
            (
                "--preview-fingerprint",
                "0" * 64,
                "--evidence-ref",
                "review:unit-test",
                "--actor",
                "operator:test",
                "--idempotency-key",
                "renewal:test",
            ),
        ),
    ),
)
def test_reviewed_renewal_cli_admits_explicit_funding_recovery_context(
    monkeypatch, subcommand, extra
):
    seen = []
    monkeypatch.setattr(
        billing_target_shadow,
        "SessionLocal",
        lambda: SimpleNamespace(close=lambda: None),
    )
    handler_name = (
        "_cmd_preview_prepaid_service_renewal"
        if subcommand.startswith("preview")
        else "_cmd_execute_reviewed_prepaid_service_renewal"
    )
    monkeypatch.setattr(
        billing_target_shadow,
        handler_name,
        lambda _db, args: seen.append(args.eligibility_context) or 0,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "billing_target_shadow",
            subcommand,
            "--subscription",
            str(uuid4()),
            "--starts-at",
            "2026-10-11T00:00:00+00:00",
            "--ends-at",
            "2026-11-11T00:00:00+00:00",
            "--amount",
            "100.00",
            "--eligibility-context",
            "funding_recovery",
            *extra,
        ],
    )

    assert billing_target_shadow.main() == 0
    assert seen == ["funding_recovery"]
