"""Captive access is a per-subscription, policy-allowed, router-ready exception.

Fast SQLite unit lane (``Base.metadata.create_all``); not deployed-schema
evidence. PostgreSQL coverage lives in
``tests/integration/test_captive_access_policy_migration.py``.
"""

from datetime import UTC, datetime, timedelta

from app.models.catalog import SubscriptionStatus
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementReason,
)
from app.models.subscriber import (
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.models.subscription_engine import SettingValueType
from app.services.account_lifecycle import suspend_subscription
from app.services.radius_projection_planner import plan_radius_projection
from app.services.walled_garden_policy import (
    WalledGardenReason,
    resolve_subscription_restriction,
    resolve_walled_garden_decision,
)
from tests.captive_access_support import (
    add_rule,
    nas_with_router,
    open_session,
    ready_network,
    residential_house_account,
    serve_from,
    set_radius_setting,
)


def _captive(db, account, subscription):
    return resolve_walled_garden_decision(
        db,
        account,
        requested_mode=AccessRestrictionMode.captive,
        subscription=subscription,
    )


def _ready(db, account, subscription):
    """Account allow rule + ready network + ready serving router."""

    residential_house_account(db, account)
    ready_network(db)
    nas, router = nas_with_router(db)
    serve_from(db, subscription, nas)
    add_rule(db, scope="account", subscriber_id=account.id)
    return nas, router


def test_allowed_subscription_on_ready_router_resolves_captive(
    db_session, subscriber_account, subscription
):
    _, router = _ready(db_session, subscriber_account, subscription)

    decision = _captive(db_session, subscriber_account, subscription)

    assert decision.effective_mode == AccessRestrictionMode.captive
    assert decision.reason == WalledGardenReason.CAPTIVE_READY
    assert decision.router_ids == (router.id,)
    assert decision.subscription_id == subscription.id
    assert decision.as_dict()["policy_scope"] == "account"


def test_raw_opt_in_flag_alone_never_grants_captive(
    db_session, subscriber_account, subscription
):
    residential_house_account(db_session, subscriber_account)
    ready_network(db_session)
    nas, _ = nas_with_router(db_session)
    serve_from(db_session, subscription, nas)
    subscriber_account.captive_redirect_enabled = True
    db_session.flush()

    decision = _captive(db_session, subscriber_account, subscription)

    assert decision.effective_mode == AccessRestrictionMode.hard_reject
    assert decision.reason == WalledGardenReason.CAPTIVE_NO_POLICY_RULE


def test_safety_rails_cannot_be_granted_by_a_rule(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    add_rule(db_session, scope="global")

    for user_type in (UserType.system_user, UserType.reseller, UserType.vendor):
        subscriber_account.user_type = user_type
        decision = _captive(db_session, subscriber_account, subscription)
        assert decision.effective_mode == AccessRestrictionMode.hard_reject
        assert decision.reason == WalledGardenReason.USER_TYPE_NOT_CUSTOMER
    subscriber_account.user_type = UserType.customer

    for status in (SubscriberStatus.disabled, SubscriberStatus.canceled):
        subscriber_account.status = status
        assert (
            _captive(db_session, subscriber_account, subscription).reason
            == WalledGardenReason.ACCOUNT_NOT_SERVICE_ELIGIBLE
        )
    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = False
    assert (
        _captive(db_session, subscriber_account, subscription).reason
        == WalledGardenReason.ACCOUNT_NOT_SERVICE_ELIGIBLE
    )
    subscriber_account.is_active = True

    for status in (
        SubscriptionStatus.disabled,
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
        SubscriptionStatus.archived,
        SubscriptionStatus.hidden,
    ):
        subscription.status = status
        assert (
            _captive(db_session, subscriber_account, subscription).reason
            == WalledGardenReason.SUBSCRIPTION_NOT_CAPTIVE_ELIGIBLE
        )


def test_business_account_can_be_enabled_deliberately(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    subscriber_account.category = SubscriberCategory.business
    db_session.flush()

    # The account rule has no category condition, so business is allowed.
    assert (
        _captive(db_session, subscriber_account, subscription).effective_mode
        == AccessRestrictionMode.captive
    )


def test_request_without_subscription_scope_fails_closed(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)

    decision = resolve_walled_garden_decision(
        db_session,
        subscriber_account,
        requested_mode=AccessRestrictionMode.captive,
        subscription=None,
    )

    assert decision.effective_mode == AccessRestrictionMode.hard_reject
    assert decision.reason == WalledGardenReason.SUBSCRIPTION_SCOPE_REQUIRED


def test_invalid_network_contract_fails_closed(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    set_radius_setting(
        db_session,
        "captive_portal_ip",
        "portal.example.test",
        SettingValueType.string,
    )

    decision = _captive(db_session, subscriber_account, subscription)

    assert decision.effective_mode == AccessRestrictionMode.hard_reject
    assert decision.reason == WalledGardenReason.CAPTIVE_PORTAL_IP_INVALID


def test_router_gate_fail_closed_cases(db_session, subscriber_account, subscription):
    residential_house_account(db_session, subscriber_account)
    ready_network(db_session)
    add_rule(db_session, scope="account", subscriber_id=subscriber_account.id)

    # No serving NAS at all.
    assert (
        _captive(db_session, subscriber_account, subscription).reason
        == WalledGardenReason.ROUTER_UNRESOLVED
    )

    # Serving NAS, router not ready (snapshot without the module).
    nas, _ = nas_with_router(db_session, ready=False)
    serve_from(db_session, subscription, nas)
    assert (
        _captive(db_session, subscriber_account, subscription).reason
        == WalledGardenReason.ROUTER_NOT_READY
    )

    # Stale snapshot of an otherwise complete module.
    stale_nas, _ = nas_with_router(
        db_session, captured_at=datetime.now(UTC) - timedelta(days=3)
    )
    serve_from(db_session, subscription, stale_nas)
    assert (
        _captive(db_session, subscriber_account, subscription).reason
        == WalledGardenReason.ROUTER_NOT_READY
    )


def test_every_serving_router_must_be_ready(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    assert (
        _captive(db_session, subscriber_account, subscription).effective_mode
        == AccessRestrictionMode.captive
    )

    # A live session on a second, not-ready router closes the gate.
    other_nas, _ = nas_with_router(db_session, ready=False)
    open_session(db_session, subscription, nas=other_nas)
    assert (
        _captive(db_session, subscriber_account, subscription).reason
        == WalledGardenReason.ROUTER_NOT_READY
    )


def test_unidentifiable_session_nas_fails_closed(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    open_session(db_session, subscription, nas_ip="198.51.100.250")

    assert (
        _captive(db_session, subscriber_account, subscription).reason
        == WalledGardenReason.ROUTER_UNRESOLVED
    )


def test_session_nas_resolved_by_ip(db_session, subscriber_account, subscription):
    residential_house_account(db_session, subscriber_account)
    ready_network(db_session)
    add_rule(db_session, scope="account", subscriber_id=subscriber_account.id)
    nas_with_router(db_session, nas_ip="198.51.100.7")
    open_session(db_session, subscription, nas_ip="198.51.100.7")

    assert (
        _captive(db_session, subscriber_account, subscription).effective_mode
        == AccessRestrictionMode.captive
    )


def test_persisted_captive_lock_drives_same_radius_projection(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    subscription.status = SubscriptionStatus.active
    lock = suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:walled-garden",
        access_mode=AccessRestrictionMode.captive,
    )

    decision = resolve_subscription_restriction(
        db_session,
        subscription,
        account=subscriber_account,
    )
    projection = plan_radius_projection(
        subscription,
        restriction_mode=decision.effective_mode if decision else None,
    )

    assert lock.access_mode == AccessRestrictionMode.captive
    assert lock.requested_access_mode == AccessRestrictionMode.captive
    assert decision is not None
    assert decision.effective_mode == AccessRestrictionMode.captive
    assert projection.mode == "captive"
    assert projection.write_password is True


def test_most_restrictive_active_lock_wins(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    subscription.status = SubscriptionStatus.active
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:captive",
        access_mode=AccessRestrictionMode.captive,
    )
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.fraud,
        source="test:fraud",
        access_mode=AccessRestrictionMode.hard_reject,
    )

    decision = resolve_subscription_restriction(db_session, subscription)

    assert decision is not None
    assert decision.effective_mode == AccessRestrictionMode.hard_reject


def test_terminal_subscription_cannot_project_persisted_captive(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    subscription.status = SubscriptionStatus.active
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:terminal-captive",
        access_mode=AccessRestrictionMode.captive,
    )
    subscription.status = SubscriptionStatus.canceled
    db_session.flush()

    decision = resolve_subscription_restriction(db_session, subscription)

    assert decision is not None
    assert decision.effective_mode == AccessRestrictionMode.hard_reject


def test_persisted_captive_lock_downgrades_when_router_becomes_not_ready(
    db_session, subscriber_account, subscription
):
    _ready(db_session, subscriber_account, subscription)
    subscription.status = SubscriptionStatus.active
    suspend_subscription(
        db_session,
        str(subscription.id),
        reason=EnforcementReason.overdue,
        source="test:captive",
        access_mode=AccessRestrictionMode.captive,
    )
    other_nas, _ = nas_with_router(db_session, ready=False)
    serve_from(db_session, subscription, other_nas)

    decision = resolve_subscription_restriction(db_session, subscription)

    assert decision is not None
    assert decision.effective_mode == AccessRestrictionMode.hard_reject
    assert decision.reason == WalledGardenReason.ROUTER_NOT_READY


def test_captive_effective_mode_requires_captive_request(
    db_session, subscriber_account, subscription
):
    import pytest

    subscription.status = SubscriptionStatus.active
    db_session.flush()
    with pytest.raises(ValueError):
        suspend_subscription(
            db_session,
            str(subscription.id),
            reason=EnforcementReason.overdue,
            source="test:invalid",
            access_mode=AccessRestrictionMode.captive,
            requested_access_mode=AccessRestrictionMode.hard_reject,
        )
