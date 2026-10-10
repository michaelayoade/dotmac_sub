"""PostgreSQL evidence for migrations 666/667 and the captive policy owner.

The schema comes from the real Alembic chain (``make test-integration``).
Revision 667 is re-run through the test's own connection after its own
downgrade, so every row it touches rolls back with the test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.captive_access_policy import CaptiveAccessRule
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.collections import (
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
from app.models.subscriber import Reseller, Subscriber, SubscriberCategory
from app.services.captive_access_policy import (
    load_captive_policy_snapshot,
    subscription_facts,
)

ROOT = Path(__file__).resolve().parents[2]
BACKFILL = ROOT / "alembic/versions/667_captive_access_policy_backfill.py"
ACTOR = "migration:667_captive_access_policy_backfill"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m667", BACKFILL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind(module: ModuleType, db_session, monkeypatch) -> ModuleType:
    context = MigrationContext.configure(db_session.connection())
    monkeypatch.setattr(module, "op", Operations(context))
    return module


def _house(db_session) -> Reseller:
    existing = db_session.scalar(select(Reseller).where(Reseller.is_house.is_(True)))
    if existing is not None:
        return existing
    house = Reseller(name=f"House {uuid4().hex[:6]}", is_house=True, is_active=True)
    db_session.add(house)
    db_session.flush()
    return house


def _account(db_session, *, opted_in: bool, category: SubscriberCategory, house):
    account = Subscriber(
        first_name="Captive",
        last_name=uuid4().hex[:6],
        email=f"captive-{uuid4().hex}@example.com",
        reseller_id=house.id,
        captive_redirect_enabled=opted_in,
    )
    account.category = category
    db_session.add(account)
    db_session.flush()
    return account


def _subscription(db_session, account, offer_id) -> Subscription:
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer_id,
        status=SubscriptionStatus.suspended,
    )
    db_session.add(subscription)
    db_session.flush()
    return subscription


def _lock(db_session, subscription, *, reason, mode) -> EnforcementLock:
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=subscription.subscriber_id,
        reason=reason,
        access_mode=mode,
        source="test:migration",
        is_active=True,
    )
    db_session.add(lock)
    db_session.flush()
    return lock


def _evidence(db_session, lock, action: FinancialAccessAction) -> None:
    consequence = FinancialAccessConsequence(
        account_id=lock.subscriber_id,
        action=action,
        requested_reason=EnforcementReason.overdue,
        origin=FinancialAccessOrigin.dunning,
        eligible=True,
        outcome="suspended",
        preview_fingerprint="0" * 64,
        idempotency_key=f"test-{uuid4().hex}",
        decision_inputs={},
        result={},
    )
    db_session.add(consequence)
    db_session.flush()
    db_session.add(
        FinancialAccessConsequenceEvidence(
            consequence_id=consequence.id,
            enforcement_lock_id=lock.id,
            operation=FinancialAccessEvidenceOperation.lock_created,
        )
    )
    db_session.flush()


def test_backfill_converts_opt_ins_and_derives_lock_requests(
    db_session, catalog_offer, monkeypatch
) -> None:
    migration = _bind(_load(), db_session, monkeypatch)
    migration.downgrade()  # isolate this test's rows from the chain's own run
    house = _house(db_session)
    residential = _account(
        db_session, opted_in=True, category=SubscriberCategory.residential, house=house
    )
    business = _account(
        db_session, opted_in=True, category=SubscriberCategory.business, house=house
    )
    not_opted = _account(
        db_session, opted_in=False, category=SubscriberCategory.residential, house=house
    )
    sub_res = _subscription(db_session, residential, catalog_offer.id)
    sub_biz = _subscription(db_session, business, catalog_offer.id)
    sub_none = _subscription(db_session, not_opted, catalog_offer.id)
    captive_lock = _lock(
        db_session,
        sub_res,
        reason=EnforcementReason.overdue,
        mode=AccessRestrictionMode.captive,
    )
    suspended_lock = _lock(
        db_session,
        sub_biz,
        reason=EnforcementReason.overdue,
        mode=AccessRestrictionMode.hard_reject,
    )
    _evidence(db_session, suspended_lock, FinancialAccessAction.suspend)
    rejected_lock = _lock(
        db_session,
        sub_none,
        reason=EnforcementReason.overdue,
        mode=AccessRestrictionMode.hard_reject,
    )
    _evidence(db_session, rejected_lock, FinancialAccessAction.reject)
    admin_lock = _lock(
        db_session,
        sub_none,
        reason=EnforcementReason.admin,
        mode=AccessRestrictionMode.hard_reject,
    )

    migration.upgrade()
    db_session.expire_all()

    rules = db_session.scalars(
        select(CaptiveAccessRule).where(CaptiveAccessRule.created_by == ACTOR)
    ).all()
    by_account = {rule.subscriber_id: rule for rule in rules}
    assert residential.id in by_account and business.id in by_account
    assert not_opted.id not in by_account
    rule = by_account[residential.id]
    assert (rule.scope, rule.effect, rule.reseller_condition) == (
        "account",
        "allow",
        "house",
    )
    assert rule.subscriber_categories == ["residential"]
    assert rule.offer_ids is None and rule.reseller_ids is None

    modes = {
        lock_id: db_session.get(EnforcementLock, lock_id).requested_access_mode
        for lock_id in (
            captive_lock.id,
            suspended_lock.id,
            rejected_lock.id,
            admin_lock.id,
        )
    }
    assert modes == {
        captive_lock.id: AccessRestrictionMode.captive,
        suspended_lock.id: AccessRestrictionMode.captive,
        rejected_lock.id: AccessRestrictionMode.hard_reject,
        admin_lock.id: None,
    }

    # Decision equivalence: the former eligibility is preserved by conditions.
    snapshot = load_captive_policy_snapshot(db_session)
    assert snapshot.resolve(
        subscription_facts(
            db_session, db_session.get(Subscription, sub_res.id), residential
        )
    ).allows
    assert not snapshot.resolve(
        subscription_facts(
            db_session, db_session.get(Subscription, sub_biz.id), business
        )
    ).allows

    audit = db_session.execute(
        text(
            "SELECT metadata FROM audit_events WHERE actor_id = :actor "
            "AND action = 'access.captive_access_policy_backfilled' "
            "ORDER BY occurred_at DESC LIMIT 1"
        ),
        {"actor": ACTOR},
    ).scalar_one()
    assert audit["account_rules_inserted"] >= 2

    # Re-running the backfill inserts no second rule per account.
    db_session.execute(
        text(
            "ALTER TABLE enforcement_locks DROP CONSTRAINT "
            "ck_enforcement_locks_effective_within_request"
        )
    )
    migration.upgrade()
    assert (
        db_session.scalar(
            text(
                "SELECT count(*) FROM captive_access_rules "
                "WHERE created_by = :actor AND subscriber_id = :id"
            ),
            {"actor": ACTOR, "id": residential.id},
        )
        == 1
    )


def test_effective_mode_cannot_exceed_the_request(db_session, catalog_offer) -> None:
    house = _house(db_session)
    account = _account(
        db_session, opted_in=False, category=SubscriberCategory.residential, house=house
    )
    subscription = _subscription(db_session, account, catalog_offer.id)
    lock = EnforcementLock(
        subscription_id=subscription.id,
        subscriber_id=account.id,
        reason=EnforcementReason.overdue,
        access_mode=AccessRestrictionMode.captive,
        requested_access_mode=AccessRestrictionMode.hard_reject,
        source="test:constraint",
        is_active=True,
    )
    savepoint = db_session.begin_nested()
    db_session.add(lock)
    with pytest.raises(IntegrityError):
        db_session.flush()
    savepoint.rollback()


def test_rule_scope_target_constraint_is_enforced(db_session) -> None:
    savepoint = db_session.begin_nested()
    db_session.add(
        CaptiveAccessRule(
            scope="account",
            effect="allow",
            plan_family="home_flex",
            reseller_condition="any",
            enabled=True,
            created_by="test",
            reason="must name exactly its scope target",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    savepoint.rollback()


def test_opt_in_after_suspension_applies_on_postgres(
    db_session, subscriber, subscription
) -> None:
    """Coordinator path on PostgreSQL: advisory lock, constraint, row locks."""

    from app.models.rbac import Role, SystemUserRole
    from app.models.system_user import SystemUser
    from app.services.account_lifecycle import suspend_subscription
    from app.services.captive_access_policy import (
        AddCaptiveAccessRule,
        CaptiveRuleSpec,
    )
    from app.services.captive_access_policy_change import (
        ApplyCaptivePolicyChangeCommand,
        PreviewCaptivePolicyChangeQuery,
        apply_captive_policy_change,
        preview_captive_policy_change,
        principal_label,
    )
    from app.services.owner_commands import CommandContext
    from tests.captive_access_support import (
        nas_with_router,
        ready_network,
        residential_house_account,
        serve_from,
    )

    residential_house_account(db_session, subscriber)
    ready_network(db_session)
    nas, _ = nas_with_router(db_session)
    serve_from(db_session, subscription, nas)
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    lock = suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:pg",
        access_mode=AccessRestrictionMode.hard_reject,
        requested_access_mode=AccessRestrictionMode.captive,
    )
    staff = SystemUser(
        first_name="Net", last_name="Ops", email=f"ops-{uuid4().hex}@example.com"
    )
    role = Role(name="admin", is_active=True)
    db_session.add_all([staff, role])
    db_session.flush()
    db_session.add(SystemUserRole(system_user_id=staff.id, role_id=role.id))
    staff_id, lock_id, account_id = staff.id, lock.id, subscriber.id
    db_session.commit()

    from app.models.captive_access_policy import (
        CaptiveAccessRuleEffect,
        CaptiveAccessRuleScope,
    )

    change = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=CaptiveAccessRuleScope.account,
            effect=CaptiveAccessRuleEffect.allow,
            reason="customer asked for portal access",
            subscriber_id=account_id,
        )
    )
    preview = preview_captive_policy_change(
        db_session, query=PreviewCaptivePolicyChangeQuery(change=change, actor="t")
    )
    db_session.rollback()
    command_id = uuid4()
    outcome = apply_captive_policy_change(
        db_session,
        ApplyCaptivePolicyChangeCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=principal_label(staff_id),
                scope="access:captive_access_policy:write",
                reason="approved",
                idempotency_key=f"pg-{uuid4().hex}",
            ),
            change=change,
            expected_preview_fingerprint=preview.preview_fingerprint,
            authorized_system_user_id=staff_id,
        ),
    )

    assert outcome.lock_updates_applied == 1
    assert outcome.moves_applied == preview.moves
    db_session.expire_all()
    assert (
        db_session.get(EnforcementLock, lock_id).access_mode
        == AccessRestrictionMode.captive
    )
