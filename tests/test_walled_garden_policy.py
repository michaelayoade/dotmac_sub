"""Captive access is an eligible, explicit, network-ready exception."""

from app.models.catalog import SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.enforcement_lock import (
    AccessRestrictionMode,
    EnforcementReason,
)
from app.models.subscriber import (
    Reseller,
    SubscriberCategory,
    SubscriberStatus,
    UserType,
)
from app.models.subscription_engine import SettingValueType
from app.services.account_lifecycle import suspend_subscription
from app.services.radius_projection_planner import plan_radius_projection
from app.services.walled_garden_policy import (
    resolve_subscription_restriction,
    resolve_walled_garden_decision,
)


def _setting(db, key: str, value: str, value_type: SettingValueType) -> None:
    db.query(DomainSetting).filter(
        DomainSetting.domain == SettingDomain.radius,
        DomainSetting.key == key,
    ).delete(synchronize_session=False)
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key=key,
            value_type=value_type,
            value_text=value,
            value_json=value.lower() == "true"
            if value_type.value == "boolean"
            else None,
            is_active=True,
        )
    )


def _ready_network(db) -> None:
    _setting(db, "captive_redirect_enabled", "true", SettingValueType.boolean)
    _setting(db, "captive_portal_ip", "203.0.113.10", SettingValueType.string)
    _setting(
        db,
        "captive_portal_url",
        "https://portal.example.test/pay",
        SettingValueType.string,
    )
    db.flush()


def _eligible_account(db, account):
    house = Reseller(name="House", code="HOUSE-WG", is_house=True, is_active=True)
    db.add(house)
    db.flush()
    account.reseller_id = house.id
    account.user_type = UserType.customer
    account.category = SubscriberCategory.residential
    account.captive_redirect_enabled = True
    account.is_active = True
    db.flush()
    return house


def test_eligible_optin_with_ready_network_resolves_captive(
    db_session, subscriber_account
):
    _eligible_account(db_session, subscriber_account)
    _ready_network(db_session)

    decision = resolve_walled_garden_decision(
        db_session,
        subscriber_account,
        requested_mode=AccessRestrictionMode.captive,
    )

    assert decision.effective_mode == AccessRestrictionMode.captive
    assert decision.reason == "captive_ready"


def test_uncategorized_business_reseller_and_nonhouse_accounts_fail_closed(
    db_session, subscriber_account
):
    house = _eligible_account(db_session, subscriber_account)
    _ready_network(db_session)

    subscriber_account.metadata_ = {}
    assert (
        resolve_walled_garden_decision(
            db_session,
            subscriber_account,
            requested_mode=AccessRestrictionMode.captive,
        ).effective_mode
        == AccessRestrictionMode.hard_reject
    )

    for category in (
        SubscriberCategory.business,
        SubscriberCategory.government,
        SubscriberCategory.ngo,
    ):
        subscriber_account.category = category
        assert (
            resolve_walled_garden_decision(
                db_session,
                subscriber_account,
                requested_mode=AccessRestrictionMode.captive,
            ).effective_mode
            == AccessRestrictionMode.hard_reject
        )

    subscriber_account.category = SubscriberCategory.residential
    subscriber_account.user_type = UserType.reseller
    assert (
        resolve_walled_garden_decision(
            db_session,
            subscriber_account,
            requested_mode=AccessRestrictionMode.captive,
        ).effective_mode
        == AccessRestrictionMode.hard_reject
    )

    subscriber_account.user_type = UserType.customer
    house.is_house = False
    assert (
        resolve_walled_garden_decision(
            db_session,
            subscriber_account,
            requested_mode=AccessRestrictionMode.captive,
        ).effective_mode
        == AccessRestrictionMode.hard_reject
    )


def test_disabled_canceled_and_inactive_accounts_fail_closed(
    db_session, subscriber_account
):
    _eligible_account(db_session, subscriber_account)
    _ready_network(db_session)

    for status in (SubscriberStatus.disabled, SubscriberStatus.canceled):
        subscriber_account.status = status
        assert (
            resolve_walled_garden_decision(
                db_session,
                subscriber_account,
                requested_mode=AccessRestrictionMode.captive,
            ).effective_mode
            == AccessRestrictionMode.hard_reject
        )

    subscriber_account.status = SubscriberStatus.active
    subscriber_account.is_active = False
    assert (
        resolve_walled_garden_decision(
            db_session,
            subscriber_account,
            requested_mode=AccessRestrictionMode.captive,
        ).effective_mode
        == AccessRestrictionMode.hard_reject
    )


def test_invalid_network_contract_fails_closed(db_session, subscriber_account):
    _eligible_account(db_session, subscriber_account)
    _setting(
        db_session,
        "captive_redirect_enabled",
        "true",
        SettingValueType.boolean,
    )
    _setting(
        db_session,
        "captive_portal_ip",
        "portal.example.test",
        SettingValueType.string,
    )
    _setting(
        db_session,
        "captive_portal_url",
        "https://portal.example.test/pay",
        SettingValueType.string,
    )
    db_session.flush()

    decision = resolve_walled_garden_decision(
        db_session,
        subscriber_account,
        requested_mode=AccessRestrictionMode.captive,
    )

    assert decision.effective_mode == AccessRestrictionMode.hard_reject
    assert decision.reason == "captive_portal_ip_invalid"


def test_persisted_captive_lock_drives_same_radius_projection(
    db_session, subscriber_account, subscription
):
    _eligible_account(db_session, subscriber_account)
    _ready_network(db_session)
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
    assert decision is not None
    assert decision.effective_mode == AccessRestrictionMode.captive
    assert projection.mode == "captive"
    assert projection.write_password is True


def test_most_restrictive_active_lock_wins(
    db_session, subscriber_account, subscription
):
    _eligible_account(db_session, subscriber_account)
    _ready_network(db_session)
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
    _eligible_account(db_session, subscriber_account)
    _ready_network(db_session)
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
