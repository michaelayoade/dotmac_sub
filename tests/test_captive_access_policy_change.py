"""Preview/apply of captive policy changes and lock re-evaluation.

Fast SQLite unit lane (``Base.metadata.create_all``); not deployed-schema
evidence. The owner command runs through the real ``execute_owner_command``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.captive_access_policy import (
    CaptiveAccessPolicyChange,
    CaptiveAccessRule,
    CaptiveAccessRuleEffect,
    CaptiveAccessRuleScope,
    CaptiveCustomerSetMember,
)
from app.models.catalog import Subscription, SubscriptionStatus
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementLock,
    EnforcementReason,
)
from app.models.event_store import EventStore
from app.models.rbac import Permission, Role, RolePermission, SystemUserRole
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.services.account_lifecycle import suspend_subscription
from app.services.captive_access_policy import (
    AddCaptiveAccessRule,
    AddCaptiveCustomerSetMembers,
    CaptivePolicyChange,
    CaptiveRuleSpec,
    CreateCaptiveCustomerSet,
    DisableCaptiveAccessRule,
    ReevaluateCaptivePolicy,
)
from app.services.captive_access_policy_change import (
    ApplyCaptivePolicyChangeCommand,
    CaptiveMoveDirection,
    CaptivePolicyChangeError,
    CaptivePolicyChangeErrorCode,
    PreviewCaptivePolicyChangeQuery,
    apply_captive_policy_change,
    preview_captive_policy_change,
    principal_label,
)
from app.services.owner_commands import CommandContext
from app.services.radius_projection_planner import plan_login_radius_projections
from app.services.walled_garden_policy import resolve_subscription_restriction
from tests.captive_access_support import (
    nas_with_router,
    ready_network,
    residential_house_account,
    serve_from,
)


def _staff(db, *, granted: bool = True) -> UUID:
    user = SystemUser(
        first_name="Net",
        last_name="Ops",
        email=f"netops-{uuid4().hex[:8]}@example.com",
        is_active=True,
    )
    db.add(user)
    db.flush()
    if granted:
        role = Role(name=f"netops-{uuid4().hex[:6]}", is_active=True)
        db.add(role)
        db.flush()
        permission = db.scalar(
            select(Permission).where(Permission.key == "network:radius:write")
        )
        if permission is None:
            permission = Permission(key="network:radius:write", is_active=True)
            db.add(permission)
            db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
        db.add(SystemUserRole(system_user_id=user.id, role_id=role.id))
    user_id = user.id
    db.commit()
    return user_id


def _suspended_before_opt_in(db, account: Subscriber, subscription: Subscription):
    """A dunning suspension that requested captive but got hard reject."""

    residential_house_account(db, account)
    ready_network(db)
    nas, router = nas_with_router(db, name=f"BNG-{uuid4().hex[:4]}")
    serve_from(db, subscription, nas)
    subscription.status = SubscriptionStatus.active
    subscription.login = f"captive-{uuid4().hex[:8]}"
    db.flush()
    lock = suspend_subscription(
        db,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:dunning",
        access_mode=AccessRestrictionMode.hard_reject,
        requested_access_mode=AccessRestrictionMode.captive,
    )
    db.commit()
    return lock, router


def _account_allow(account: Subscriber) -> AddCaptiveAccessRule:
    return AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=CaptiveAccessRuleScope.account,
            effect=CaptiveAccessRuleEffect.allow,
            reason="customer asked for portal access",
            subscriber_id=account.id,
        )
    )


def _preview(db, change: CaptivePolicyChange):
    db.rollback()
    preview = preview_captive_policy_change(
        db, query=PreviewCaptivePolicyChangeQuery(change=change, actor="test")
    )
    db.rollback()  # adapters close read transactions; keep the session free
    return preview


def _apply(
    db,
    change: CaptivePolicyChange,
    *,
    staff: UUID,
    fingerprint: str,
    key: str | None = None,
    max_subscriptions: int = 200,
):
    command_id = uuid4()
    db.rollback()  # the adapter hands the owner a transaction-free session
    return apply_captive_policy_change(
        db,
        ApplyCaptivePolicyChangeCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=principal_label(staff),
                scope="access:captive_access_policy:write",
                reason="approved change",
                idempotency_key=key or f"key-{uuid4().hex}",
            ),
            change=change,
            expected_preview_fingerprint=fingerprint,
            authorized_system_user_id=staff,
            max_subscriptions=max_subscriptions,
        ),
    )


def test_opt_in_after_suspension_moves_lock_to_captive_and_reprojects(
    db_session, subscriber_account, subscription
):
    lock, router = _suspended_before_opt_in(
        db_session, subscriber_account, subscription
    )
    staff = _staff(db_session)
    change = _account_allow(subscriber_account)

    preview = _preview(db_session, change)

    assert [item.lock_id for item in preview.lock_updates] == [lock.id]
    assert preview.lock_updates[0].to_mode == AccessRestrictionMode.captive
    assert [item.subscription_id for item in preview.to_captive] == [subscription.id]
    assert preview.to_hard_reject == ()
    assert preview.to_captive[0].router_names == (router.name,)
    assert {(c.direction, c.key, c.count) for c in preview.by_router} == {
        (CaptiveMoveDirection.to_captive, router.name, 1)
    }
    # Preview wrote nothing.
    assert db_session.scalar(select(CaptiveAccessRule.id)) is None
    db_session.rollback()

    outcome = _apply(
        db_session, change, staff=staff, fingerprint=preview.preview_fingerprint
    )

    assert outcome.lock_updates_applied == 1
    assert outcome.moves_applied == preview.moves  # preview == apply
    assert outcome.remaining_subscriptions == 0
    assert outcome.rule_id is not None
    db_session.expire_all()
    stored = db_session.get(EnforcementLock, lock.id)
    assert stored.access_mode == AccessRestrictionMode.captive
    assert stored.requested_access_mode == AccessRestrictionMode.captive
    events = db_session.scalars(
        select(EventStore).where(
            EventStore.event_type == "enforcement_lock.access_mode_changed"
        )
    ).all()
    assert [event.payload["lock_id"] for event in events] == [str(lock.id)]
    assert events[0].payload["to_access_mode"] == "captive"

    # The canonical restriction and the RADIUS plan both follow the lock.
    refreshed = db_session.get(Subscription, subscription.id)
    decision = resolve_subscription_restriction(db_session, refreshed)
    assert decision is not None
    assert decision.effective_mode == AccessRestrictionMode.captive
    plans = plan_login_radius_projections(
        db_session, [refreshed], include_test_access=False
    )
    assert plans[refreshed.login].plan.mode == "captive"
    assert plans[refreshed.login].plan.write_password is True


def test_apply_is_idempotent_and_conflicting_key_reuse_is_refused(
    db_session, subscriber_account, subscription
):
    _suspended_before_opt_in(db_session, subscriber_account, subscription)
    staff = _staff(db_session)
    change = _account_allow(subscriber_account)
    preview = _preview(db_session, change)

    first = _apply(
        db_session,
        change,
        staff=staff,
        fingerprint=preview.preview_fingerprint,
        key="CHG-1",
    )
    replay = _apply(
        db_session,
        change,
        staff=staff,
        fingerprint=preview.preview_fingerprint,
        key="CHG-1",
    )

    assert replay.replayed is True
    assert replay.change_id == first.change_id
    assert replay.moves_applied == first.moves_applied
    assert len(db_session.scalars(select(CaptiveAccessRule)).all()) == 1
    assert len(db_session.scalars(select(CaptiveAccessPolicyChange)).all()) == 1

    with pytest.raises(CaptivePolicyChangeError) as exc:
        _apply(
            db_session,
            ReevaluateCaptivePolicy(),
            staff=staff,
            fingerprint=preview.preview_fingerprint,
            key="CHG-1",
        )
    assert exc.value.code == CaptivePolicyChangeErrorCode.IDEMPOTENCY_CONFLICT


def test_stale_preview_is_refused_without_writes(
    db_session, subscriber_account, subscription
):
    _suspended_before_opt_in(db_session, subscriber_account, subscription)
    staff = _staff(db_session)
    change = _account_allow(subscriber_account)
    preview = _preview(db_session, change)
    # Router readiness changes between preview and apply.
    other_nas, _ = nas_with_router(db_session, ready=False)
    serve_from(db_session, db_session.get(Subscription, subscription.id), other_nas)
    db_session.commit()

    with pytest.raises(CaptivePolicyChangeError) as exc:
        _apply(db_session, change, staff=staff, fingerprint=preview.preview_fingerprint)

    assert exc.value.code == CaptivePolicyChangeErrorCode.STALE_PREVIEW
    assert db_session.scalar(select(CaptiveAccessRule.id)) is None


def test_unauthorized_principal_is_refused(
    db_session, subscriber_account, subscription
):
    _suspended_before_opt_in(db_session, subscriber_account, subscription)
    staff = _staff(db_session, granted=False)
    change = _account_allow(subscriber_account)
    preview = _preview(db_session, change)

    with pytest.raises(CaptivePolicyChangeError) as exc:
        _apply(db_session, change, staff=staff, fingerprint=preview.preview_fingerprint)

    assert exc.value.code == CaptivePolicyChangeErrorCode.PERMISSION_DENIED
    assert db_session.scalar(select(CaptiveAccessRule.id)) is None


def test_disabling_the_rule_moves_captive_back_to_hard_reject(
    db_session, subscriber_account, subscription
):
    lock, _ = _suspended_before_opt_in(db_session, subscriber_account, subscription)
    staff = _staff(db_session)
    grant = _account_allow(subscriber_account)
    applied = _apply(
        db_session,
        grant,
        staff=staff,
        fingerprint=_preview(db_session, grant).preview_fingerprint,
    )
    assert applied.rule_id is not None
    revoke = DisableCaptiveAccessRule(
        rule_id=applied.rule_id, reason="customer withdrew consent"
    )

    preview = _preview(db_session, revoke)
    outcome = _apply(
        db_session, revoke, staff=staff, fingerprint=preview.preview_fingerprint
    )

    assert [move.direction for move in outcome.moves_applied] == [
        CaptiveMoveDirection.to_hard_reject
    ]
    db_session.expire_all()
    assert (
        db_session.get(EnforcementLock, lock.id).access_mode
        == AccessRestrictionMode.hard_reject
    )


def test_hard_reject_requests_are_never_upgraded(
    db_session, subscriber_account, subscription
):
    residential_house_account(db_session, subscriber_account)
    ready_network(db_session)
    nas, _ = nas_with_router(db_session)
    serve_from(db_session, subscription, nas)
    subscription.status = SubscriptionStatus.active
    db_session.flush()
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="test:fraud",
    )
    db_session.commit()

    preview = _preview(db_session, _account_allow(subscriber_account))

    assert preview.subscriptions_evaluated == 0
    assert preview.lock_updates == ()


def test_batches_are_bounded_and_drained_by_reevaluation(
    db_session, subscriber_account, subscription, catalog_offer
):
    from app.schemas.catalog import SubscriptionCreate
    from app.services import catalog as catalog_service

    _suspended_before_opt_in(db_session, subscriber_account, subscription)
    nas_id = db_session.get(Subscription, subscription.id).provisioning_nas_device_id
    for index in range(2):
        account = Subscriber(
            first_name="Extra",
            last_name=str(index),
            email=f"extra-{uuid4().hex[:8]}@example.com",
        )
        db_session.add(account)
        db_session.commit()
        residential_house_account(db_session, account)
        account_id = account.id
        db_session.commit()
        extra = catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                account_id=account_id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
            ),
        )
        extra_id = extra.id
        db_session.commit()
        extra = db_session.get(Subscription, extra_id)
        extra.provisioning_nas_device_id = nas_id
        db_session.flush()
        suspend_subscription(
            db_session,
            str(extra_id),
            reason=EnforcementReason.overdue,
            source="test:dunning",
            access_mode=AccessRestrictionMode.hard_reject,
            requested_access_mode=AccessRestrictionMode.captive,
        )
        db_session.commit()
    staff = _staff(db_session)
    change = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=CaptiveAccessRuleScope.global_,
            effect=CaptiveAccessRuleEffect.allow,
            reason="captive for every eligible service",
        )
    )
    preview = _preview(db_session, change)
    assert len(preview.to_captive) == 3

    first = _apply(
        db_session,
        change,
        staff=staff,
        fingerprint=preview.preview_fingerprint,
        max_subscriptions=1,
    )
    assert len(first.subscriptions_applied) == 1
    assert first.remaining_subscriptions == 2

    drained = 0
    for _ in range(3):
        step = _preview(db_session, ReevaluateCaptivePolicy())
        if not step.lock_updates:
            break
        outcome = _apply(
            db_session,
            ReevaluateCaptivePolicy(),
            staff=staff,
            fingerprint=step.preview_fingerprint,
            max_subscriptions=1,
        )
        drained += len(outcome.subscriptions_applied)
    assert drained == 2
    assert _preview(db_session, ReevaluateCaptivePolicy()).lock_updates == ()


def test_customer_set_cohort_changes_flow_through_the_coordinator(
    db_session, subscriber_account, subscription
):
    _suspended_before_opt_in(db_session, subscriber_account, subscription)
    staff = _staff(db_session)
    create = CreateCaptiveCustomerSet(name="Lekki pilot", reason="pilot cohort")
    created = _apply(
        db_session,
        create,
        staff=staff,
        fingerprint=_preview(db_session, create).preview_fingerprint,
    )
    assert created.customer_set_id is not None
    rule = AddCaptiveAccessRule(
        rule=CaptiveRuleSpec(
            scope=CaptiveAccessRuleScope.customer_set,
            effect=CaptiveAccessRuleEffect.allow,
            reason="pilot cohort gets the portal",
            customer_set_id=created.customer_set_id,
        )
    )
    no_members = _preview(db_session, rule)
    assert no_members.moves == ()
    _apply(db_session, rule, staff=staff, fingerprint=no_members.preview_fingerprint)

    add = AddCaptiveCustomerSetMembers(
        customer_set_id=created.customer_set_id,
        subscriber_ids=frozenset({subscriber_account.id}),
        reason="joined the pilot",
    )
    preview = _preview(db_session, add)
    outcome = _apply(
        db_session, add, staff=staff, fingerprint=preview.preview_fingerprint
    )

    assert outcome.members_added == 1
    assert [move.subscription_id for move in outcome.moves_applied] == [subscription.id]
    assert (
        db_session.scalar(
            select(CaptiveCustomerSetMember.subscriber_id).where(
                CaptiveCustomerSetMember.removed_at.is_(None)
            )
        )
        == subscriber_account.id
    )


def test_enforcement_handler_reprojects_and_refreshes_sessions(
    db_session, subscriber_account, subscription, monkeypatch
):
    from app.services.events.handlers.enforcement import EnforcementHandler
    from app.services.events.types import Event, EventType

    subscription.status = SubscriptionStatus.suspended
    db_session.flush()
    calls: list[tuple[str, str]] = []
    handler = EnforcementHandler()
    monkeypatch.setattr(
        handler,
        "_enforce_subscription_block",
        lambda db, subscription_id, *, reason="suspended", **_: calls.append(
            (subscription_id, reason)
        ),
    )

    handler.handle(
        db_session,
        Event(
            event_type=EventType.enforcement_lock_access_mode_changed,
            payload={"subscription_id": str(subscription.id)},
            subscription_id=subscription.id,
        ),
    )

    assert calls == [(str(subscription.id), "captive_policy_change")]
