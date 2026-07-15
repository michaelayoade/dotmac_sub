"""Grace timing has one precedence and one date-based decision contract."""

from datetime import UTC, datetime

from app.models.catalog import BillingMode, PolicySet
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.collections.grace_policy import (
    EffectiveGracePolicy,
    decide_grace,
    resolve_effective_grace_policy,
)
from app.services.web_catalog_settings import policy_set_form_context


def _billing_grace_default(db, *, mode: BillingMode, days: int) -> None:
    key = f"{mode.value}_default_grace_period_days"
    db.query(DomainSetting).filter(
        DomainSetting.domain == SettingDomain.billing,
        DomainSetting.key == key,
    ).delete(synchronize_session=False)
    db.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key=key,
            value_type=SettingValueType.integer,
            value_text=str(days),
            is_active=True,
        )
    )
    db.flush()


def test_grace_precedence_is_account_then_policy_then_billing_default(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    policy = PolicySet(name="Prepaid grace", grace_days=5, is_active=True)
    db_session.add(policy)
    db_session.flush()
    subscriber_account.policy_set_id = policy.id
    _billing_grace_default(db_session, mode=BillingMode.prepaid, days=9)

    inherited = resolve_effective_grace_policy(db_session, subscriber_account)
    assert (inherited.days, inherited.source, inherited.policy_set_id) == (
        5,
        "policy_set",
        policy.id,
    )

    subscriber_account.grace_period_days = 2
    overridden = resolve_effective_grace_policy(db_session, subscriber_account)
    assert (overridden.days, overridden.source) == (2, "account_override")

    subscriber_account.grace_period_days = None
    subscriber_account.policy_set_id = None
    policy.is_active = False
    defaulted = resolve_effective_grace_policy(
        db_session,
        subscriber_account,
        policy_set_id=policy.id,
    )
    assert (defaulted.days, defaulted.source) == (9, "billing_mode_default")


def test_dunning_offsets_begin_after_grace_end():
    policy = EffectiveGracePolicy(
        days=7,
        source="policy_set",
        billing_mode=BillingMode.postpaid,
        policy_set_id=None,
    )
    due_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    last_grace_day = decide_grace(
        policy,
        starts_at=due_at,
        as_of=datetime(2026, 7, 8, 23, 59, tzinfo=UTC),
    )
    first_action_day = decide_grace(
        policy,
        starts_at=due_at,
        as_of=datetime(2026, 7, 9, 0, 1, tzinfo=UTC),
    )

    assert last_grace_day.phase == "in_grace"
    assert last_grace_day.elapsed_days_after_grace == 0
    assert last_grace_day.ends_at == datetime(2026, 7, 9, 0, 0, tzinfo=UTC)
    assert first_action_day.phase == "actionable"
    assert first_action_day.elapsed_days_after_grace == 1


def test_policy_form_preserves_explicit_zero_grace(db_session):
    policy = PolicySet(name="No grace", grace_days=0, is_active=True)
    db_session.add(policy)
    db_session.commit()

    context = policy_set_form_context(db_session, policy_id=str(policy.id))

    assert context is not None
    assert context["policy"]["grace_days"] == 0
