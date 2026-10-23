"""The single rate owner (``access.rate_profile``), its readers and its guards.

Covers the derivation layers, the import-residue reproduction (a stale
credential profile stays wrong under the old precedence and is derived
correctly by the owner), the D1/D2/D6 decisions, the preflight and the
switch-off parity of every reader.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.models.catalog import (
    AccessCredential,
    AccessType,
    BillingMode,
    CatalogOffer,
    GuaranteedSpeedType,
    NasVendor,
    OfferRadiusProfile,
    PriceBasis,
    RadiusProfile,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.fup_state import FupActionStatus, FupState
from app.models.radius import RadiusUser
from app.models.subscriber import Subscriber
from app.models.subscription_change import (
    SubscriptionChangeExecutionState,
    SubscriptionChangeRequest,
)
from app.schemas.catalog import AccessCredentialUpdate
from app.schemas.settings import DomainSettingUpdate
from app.services import access_rate_profile as owner
from app.services import radius_population, settings_spec
from app.services.catalog.credentials import AccessCredentials
from app.services.credential_crypto import (
    encrypt_credential_with_key,
    get_encryption_key,
)
from app.services.domain_errors import DomainError
from app.services.domain_settings import DomainSettings

# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def owner_on(monkeypatch):
    monkeypatch.setattr(owner, "rate_owner_enabled", lambda _db: True)


@pytest.fixture()
def owner_off(monkeypatch):
    monkeypatch.setattr(owner, "rate_owner_enabled", lambda _db: False)


@pytest.fixture()
def _radius_env(monkeypatch, db_session):
    monkeypatch.setattr(
        radius_population,
        "active_external_radius_targets",
        lambda _db, capability=None: [
            {"target_name": "test", "target_fingerprint": "test-target"}
        ],
    )
    monkeypatch.setattr(
        radius_population, "assert_legacy_target_alignment", lambda _db: []
    )
    monkeypatch.setattr(
        radius_population, "simultaneous_use_enforcement_enabled", lambda _db: True
    )
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(radius_population, "SessionLocal", lambda: db_session)


def _profile(db, name, *, rate=None, down=None, up=None, code=None):
    profile = RadiusProfile(
        name=name,
        code=code or name.lower().replace(" ", "-"),
        vendor=NasVendor.mikrotik,
        mikrotik_rate_limit=rate,
        download_speed=down,
        upload_speed=up,
        is_active=True,
    )
    db.add(profile)
    db.commit()
    return profile


def _offer(db, name, *, down=10, up=10, profile=None, **extra):
    offer = CatalogOffer(
        name=f"{name} {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle="monthly",
        speed_download_mbps=down,
        speed_upload_mbps=up,
        **extra,
    )
    db.add(offer)
    db.commit()
    if profile is not None:
        db.add(OfferRadiusProfile(offer_id=offer.id, profile_id=profile.id))
        db.commit()
    return offer


def _account(db):
    account = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        billing_mode=BillingMode.prepaid,
    )
    db.add(account)
    db.commit()
    return account


def _service(
    db,
    offer,
    *,
    sub_profile=None,
    slot=None,
    pre_throttle=None,
    account=None,
):
    account = account or _account(db)
    login = f"u{uuid.uuid4().hex[:8]}"
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        login=login,
        radius_profile_id=sub_profile.id if sub_profile else None,
    )
    db.add(subscription)
    db.flush()
    credential = AccessCredential(
        subscriber_id=account.id,
        subscription_id=subscription.id,
        username=login,
        is_active=True,
        secret_hash=encrypt_credential_with_key("pw-" + login, get_encryption_key()),
        radius_profile_id=slot.id if slot else None,
        pre_throttle_radius_profile_id=pre_throttle.id if pre_throttle else None,
    )
    db.add(credential)
    db.commit()
    return subscription, credential


def _fup_throttle(db, subscription, profile):
    state = FupState(
        subscription_id=subscription.id,
        offer_id=subscription.offer_id,
        action_status=FupActionStatus.throttled,
        throttle_profile_id=profile.id,
    )
    db.add(state)
    db.commit()
    return state


def _set_collections_profile(db, profile):
    DomainSettings(SettingDomain.collections).upsert_by_key(
        db,
        "throttle_radius_profile_id",
        DomainSettingUpdate(
            value_type=SettingValueType.string, value_text=str(profile.id)
        ),
    )


def _borrow_session(monkeypatch, db_session):
    """Let the CLI use the test session without ending it."""
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "rollback", lambda: None)
    monkeypatch.setattr(db_session, "close", lambda: None)


def _sweep_rates(monkeypatch, *, only=None):
    """Run the dry-run sweep and return {login: (rate, test_rate, group)}."""
    items = {}

    def spy(item, *args, **kwargs):
        items[item.username] = item
        return [], [], []

    monkeypatch.setattr(radius_population, "_projection_rows_for_item", spy)
    radius_population.populate(
        dry_run=True,
        only_usernames=only,
        include_expected_fingerprints=True,
    )

    def rate(attrs):
        return next((a[2] for a in attrs if a[0] == "Mikrotik-Rate-Limit"), None)

    return {
        login: (rate(item.reply_attrs), rate(item.test_reply_attrs), item.profile_group)
        for login, item in items.items()
    }


# ------------------------------------------------------------ the switch


def test_switch_is_registered_off_by_default():
    spec = settings_spec.get_spec(SettingDomain.radius, "rate_owner_enabled")
    assert spec is not None
    assert spec.default is False


def test_switch_reads_the_domain_setting(db_session):
    assert owner.rate_owner_enabled(db_session) is False
    db_session.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key="rate_owner_enabled",
            value_type=SettingValueType.boolean,
            value_json=True,
            is_active=True,
        )
    )
    db_session.commit()
    assert owner.rate_owner_enabled(db_session) is True


# --------------------------------------------- import residue reproduction


def test_import_residue_old_precedence_keeps_the_stale_rate(
    db_session, _radius_env, owner_off, monkeypatch
):
    """A credential profile from the import (no throttle markers) wins today."""
    ten = _profile(db_session, "Ten", rate="10M/10M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    offer_a = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer_a, sub_profile=ten, slot=fifteen)

    rates = _sweep_rates(monkeypatch)

    assert rates[sub.login][0] == "15M/15M"  # characterises the bug


def test_import_residue_owner_derives_the_package_rate(
    db_session, _radius_env, owner_on, monkeypatch
):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    offer_a = _offer(db_session, "A", profile=ten)
    offer_b = _offer(db_session, "B", profile=twenty)
    sub, credential = _service(db_session, offer_a, sub_profile=ten, slot=fifteen)
    sub_id, credential_id, login = sub.id, credential.id, sub.login
    offer_b_id = offer_b.id

    decision = owner.resolve_effective_rate(db_session, sub, credential)
    assert decision.layer is owner.RateLayer.plan
    assert decision.rate_limit == "10M/10M"
    assert _sweep_rates(monkeypatch)[login][0] == "10M/10M"

    # After a plan change A -> B the rate is B's, whatever the slot carries.
    # (The sweep ends its session, so reload the rows.)
    sub = db_session.get(Subscription, sub_id)
    credential = db_session.get(AccessCredential, credential_id)
    sub.offer_id = offer_b_id
    db_session.commit()
    db_session.expire_all()
    assert (
        owner.resolve_effective_rate(db_session, sub, credential).rate_limit
        == "20M/20M"
    )


def test_coa_carries_the_same_rate_as_the_sweep(
    db_session, _radius_env, owner_on, monkeypatch
):
    from app.models.usage import AccountingStatus, RadiusAccountingSession
    from app.services import enforcement

    ten = _profile(db_session, "Ten", rate="10M/10M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, sub_profile=ten, slot=fifteen)
    db_session.add(
        RadiusAccountingSession(
            subscription_id=sub.id,
            access_credential_id=credential.id,
            session_id="s1",
            status_type=AccountingStatus.start,
        )
    )
    db_session.commit()

    sent = []
    monkeypatch.setattr(
        enforcement,
        "_send_coa_update",
        lambda db, nas, user, ip, sid, profile, rate=None: sent.append(rate) or True,
    )
    monkeypatch.setattr(enforcement, "_resolve_nas_device", lambda db, s: object())
    monkeypatch.setattr(
        "app.services.test_connection.access_for_subscription",
        lambda db, sid: None,
    )

    enforcement.update_subscription_sessions(db_session, str(sub.id))

    assert sent == ["10M/10M"]
    assert _sweep_rates(monkeypatch)[sub.login][0] == sent[0]


def test_coa_without_a_profile_uses_the_offer_rate_with_committed_floor(
    db_session, owner_on
):
    offer = _offer(
        db_session,
        "Dedicated",
        down=100,
        up=50,
        guaranteed_speed=GuaranteedSpeedType.fixed,
        guaranteed_speed_limit_at=20,
    )
    sub, credential = _service(db_session, offer)

    decision = owner.resolve_effective_rate(db_session, sub, credential)

    assert decision.profile is None
    assert decision.rate_limit == "50M/100M 0/0 0/0 0/0 8 20M/20M"


# --------------------------------------------------------- throttle layers


def test_collections_marker_is_recognised_only_with_a_marker(db_session, owner_on):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    slow = _profile(db_session, "Slow", rate="1M/1M", down=1000, up=1000)
    stale = _profile(db_session, "Stale", rate="7M/7M")
    _set_collections_profile(db_session, slow)
    offer = _offer(db_session, "A", profile=ten)

    marked, c1 = _service(db_session, offer, slot=slow, pre_throttle=ten)
    by_slot, c2 = _service(db_session, offer, slot=slow)
    inherited, c3 = _service(db_session, offer, pre_throttle=ten)  # NULL slot
    residue, c4 = _service(db_session, offer, slot=stale)

    for sub, cred, layer, rate in (
        (marked, c1, owner.RateLayer.collections_throttle, "1M/1M"),
        (by_slot, c2, owner.RateLayer.collections_throttle, "1M/1M"),
        (inherited, c3, owner.RateLayer.collections_throttle, "1M/1M"),
        (residue, c4, owner.RateLayer.plan, "10M/10M"),
    ):
        decision = owner.resolve_effective_rate(db_session, sub, cred)
        assert (decision.layer, decision.rate_limit) == (layer, rate)


def test_plan_change_while_throttled_stays_throttled_then_new_plan(
    db_session, owner_on
):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    slow = _profile(db_session, "Slow", rate="1M/1M", down=1000)
    _set_collections_profile(db_session, slow)
    offer_a = _offer(db_session, "A", profile=ten)
    offer_b = _offer(db_session, "B", profile=twenty)
    sub, credential = _service(db_session, offer_a, slot=slow, pre_throttle=ten)

    sub.offer_id = offer_b.id
    db_session.commit()
    during = owner.resolve_effective_rate(db_session, sub, credential)
    assert during.layer is owner.RateLayer.collections_throttle

    credential.radius_profile_id = None
    credential.pre_throttle_radius_profile_id = None
    db_session.commit()
    after = owner.resolve_effective_rate(db_session, sub, credential)
    assert (after.layer, after.rate_limit) == (owner.RateLayer.plan, "20M/20M")


@pytest.mark.parametrize(
    ("fup_down", "collections_down", "winner"),
    [
        (500, 1000, owner.RateLayer.fup_throttle),
        (2000, 1000, owner.RateLayer.collections_throttle),
        (1000, 1000, owner.RateLayer.collections_throttle),  # tie: collections
    ],
)
def test_overlapping_throttles_apply_the_more_restrictive(
    db_session, owner_on, fup_down, collections_down, winner
):
    """Decision D2."""
    ten = _profile(db_session, "Ten", rate="10M/10M")
    fup = _profile(
        db_session, "Fup", rate=f"{fup_down}k/{fup_down}k", down=fup_down, code="fup"
    )
    coll = _profile(
        db_session,
        "Coll",
        rate=f"{collections_down}k/{collections_down}k",
        down=collections_down,
        code="coll",
    )
    _set_collections_profile(db_session, coll)
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=coll, pre_throttle=ten)
    _fup_throttle(db_session, sub, fup)

    decision = owner.resolve_effective_rate(db_session, sub, credential)

    assert decision.layer is winner
    assert decision.state is owner.AccessState.RESTRICT
    assert decision.signals.fup_restrict and decision.signals.collections_restrict


def test_fup_throttle_alone_and_full_speed_without_enforcement(db_session, owner_on):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    fup = _profile(db_session, "Fup", rate="5M/5M", down=5000, code="fup")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer)
    _fup_throttle(db_session, sub, fup)

    assert owner.resolve_effective_rate(db_session, sub, credential).layer is (
        owner.RateLayer.fup_throttle
    )
    plan_only = owner.resolve_effective_rate(
        db_session, sub, credential, include_enforcement=False
    )
    assert (plan_only.layer, plan_only.rate_limit) == (owner.RateLayer.plan, "10M/10M")


def test_sweep_applies_the_more_restrictive_throttle(
    db_session, _radius_env, owner_on, monkeypatch
):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    fup = _profile(db_session, "Fup", rate="500k/500k", down=500, code="fup")
    coll = _profile(db_session, "Coll", rate="1M/1M", down=1000, code="coll")
    _set_collections_profile(db_session, coll)
    offer = _offer(db_session, "A", profile=ten)
    sub, _ = _service(db_session, offer, slot=coll, pre_throttle=ten)
    _fup_throttle(db_session, sub, fup)

    rate, _test_rate, group = _sweep_rates(monkeypatch)[sub.login]

    assert rate == "500k/500k"
    assert group == "Fup"


# ----------------------------------------------------- staged plan change


def _staged(db, sub, target, *, requested_at, state):
    other = _offer(db, "Other")
    request = SubscriptionChangeRequest(
        subscription_id=sub.id,
        current_offer_id=sub.offer_id,
        requested_offer_id=other.id,
        effective_date=date(2026, 1, 1),
        execution_state=state,
        remote_radius_profile_id=target.id,
        remote_reprovision_requested_at=requested_at,
    )
    db.add(request)
    db.commit()
    return request


def test_staged_plan_change_then_recovery_and_mirror(db_session, owner_on):
    from app.services.radius import ensure_radius_users_for_subscription

    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, sub_profile=ten)
    request = _staged(
        db_session,
        sub,
        twenty,
        requested_at=datetime.now(UTC),
        state=SubscriptionChangeExecutionState.provisioning,
    )

    staged = owner.resolve_effective_rate(db_session, sub, credential)
    assert staged.layer is owner.RateLayer.plan_change_staged
    assert staged.profile_id == twenty.id

    ensure_radius_users_for_subscription(db_session, sub)
    db_session.commit()
    mirror = db_session.query(RadiusUser).one()
    assert mirror.radius_profile_id == twenty.id

    # Recovery clears the staging timestamp: the old plan applies again.
    request.remote_reprovision_requested_at = None
    db_session.commit()
    recovered = owner.resolve_effective_rate(db_session, sub, credential)
    assert recovered.layer is owner.RateLayer.plan
    assert recovered.profile_id == ten.id
    ensure_radius_users_for_subscription(db_session, sub)
    db_session.commit()
    assert db_session.query(RadiusUser).one().radius_profile_id == ten.id


def test_a_staged_change_does_not_outrank_a_throttle(db_session, owner_on):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    fup = _profile(db_session, "Fup", rate="1M/1M", down=1000, code="fup")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer)
    _staged(
        db_session,
        sub,
        twenty,
        requested_at=datetime.now(UTC),
        state=SubscriptionChangeExecutionState.provisioning,
    )
    _fup_throttle(db_session, sub, fup)

    assert owner.resolve_effective_rate(db_session, sub, credential).layer is (
        owner.RateLayer.fup_throttle
    )


def test_ambiguous_package_link_is_deterministic(db_session, owner_on):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    twelve = _profile(db_session, "Twelve", rate="12M/12M")
    offer = _offer(db_session, "A", profile=ten)
    db_session.add(OfferRadiusProfile(offer_id=offer.id, profile_id=twelve.id))
    db_session.commit()
    sub, credential = _service(db_session, offer, sub_profile=twelve)

    picks = {
        owner.resolve_effective_rate(db_session, sub, credential).profile_id
        for _ in range(5)
    }

    assert picks == {twelve.id}  # the subscription's own pick when it is a link
    assert owner.resolve_effective_rate(db_session, sub, credential).plan_ambiguous


# -------------------------------------------------------- switch-off parity


def test_switch_off_sweep_keeps_the_previous_precedence(
    db_session, _radius_env, owner_off, monkeypatch
):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    slow = _profile(db_session, "Slow", rate="1M/1M")
    offer = _offer(db_session, "A", down=100, up=100, profile=ten)
    no_link = _offer(db_session, "N", down=30, up=20)
    cases = {
        "cred_wins": _service(db_session, offer, sub_profile=ten, slot=fifteen)[0],
        "sub_only": _service(db_session, offer, sub_profile=fifteen)[0],
        "none_offer_speed": _service(db_session, no_link)[0],
    }
    throttled = _service(db_session, offer, sub_profile=ten, slot=slow)[0]
    fup = _profile(db_session, "Fup", rate="500k/500k", down=500, code="fup")
    _fup_throttle(db_session, throttled, fup)  # ignored with the switch off

    rates = _sweep_rates(monkeypatch)

    assert rates[cases["cred_wins"].login][0] == "15M/15M"
    assert rates[cases["sub_only"].login][0] == "15M/15M"
    assert rates[cases["none_offer_speed"].login][0] == "20M/30M"
    assert rates[throttled.login][0] == "1M/1M"


def test_switch_off_resolvers_and_mirror_are_unchanged(db_session, owner_off):
    from app.services import enforcement
    from app.services.radius import ensure_radius_users_for_subscription

    ten = _profile(db_session, "Ten", rate="10M/10M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=fifteen)

    assert enforcement._resolve_effective_profile(db_session, sub, credential).id == (
        fifteen.id
    )
    assert enforcement._resolve_effective_profile(db_session, sub).id == ten.id
    assert enforcement._resolve_plan_profile(db_session, sub).id == ten.id
    ensure_radius_users_for_subscription(db_session, sub)
    db_session.commit()
    assert db_session.query(RadiusUser).one().radius_profile_id == fifteen.id


def test_switch_off_guards_are_inert(db_session, owner_off):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    other = _profile(db_session, "Other", rate="3M/3M")
    slow = _profile(db_session, "Slow", rate="1M/1M")
    offer = _offer(db_session, "A", profile=ten)
    _service(db_session, offer, slot=slow, pre_throttle=ten)
    _set_collections_profile(db_session, slow)

    owner.refuse_unrecorded_override(db_session, offer_id=offer.id, profile_id=other.id)
    # D6: changing the throttle profile is allowed while the owner is off.
    _set_collections_profile(db_session, other)


def test_switch_off_leaves_the_rate_grammar_untouched():
    from app.services import enforcement

    profile = RadiusProfile(
        name="p", download_speed=2000, upload_speed=1000, mikrotik_rate_limit=None
    )
    assert enforcement._build_mikrotik_rate_limit(profile) == "1000k/2000k"
    assert radius_population._rate_limit is owner.legacy_rate_limit


# ------------------------------------------------------------------ guards


def test_d1_refuses_a_non_package_profile(db_session, owner_on):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    other = _profile(db_session, "Other", rate="3M/3M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=ten)

    owner.refuse_unrecorded_override(db_session, offer_id=offer.id, profile_id=None)
    owner.refuse_unrecorded_override(db_session, offer_id=offer.id, profile_id=ten.id)
    with pytest.raises(DomainError) as excinfo:
        owner.refuse_unrecorded_override(
            db_session, offer_id=offer.id, profile_id=other.id
        )
    assert excinfo.value.code == "access.rate_profile.override_not_supported"

    # The REST credential update refuses it too, and accepts the package's own.
    with pytest.raises(DomainError):
        AccessCredentials.update(
            db_session,
            str(credential.id),
            AccessCredentialUpdate(radius_profile_id=other.id),
        )
    AccessCredentials.update(
        db_session,
        str(credential.id),
        AccessCredentialUpdate(radius_profile_id=ten.id),
    )


def test_d1_refuses_the_subscription_api(db_session, owner_on):
    from app.schemas.catalog import SubscriptionUpdate
    from app.services.catalog.subscriptions import Subscriptions

    ten = _profile(db_session, "Ten", rate="10M/10M")
    other = _profile(db_session, "Other", rate="3M/3M")
    offer = _offer(db_session, "A", profile=ten)
    sub, _ = _service(db_session, offer, sub_profile=ten)

    with pytest.raises(DomainError) as excinfo:
        Subscriptions.update(
            db_session, str(sub.id), SubscriptionUpdate(radius_profile_id=other.id)
        )
    assert excinfo.value.code == "access.rate_profile.override_not_supported"


def test_d6_refuses_changing_the_throttle_profile_while_in_use(db_session, owner_on):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    slow = _profile(db_session, "Slow", rate="1M/1M")
    slower = _profile(db_session, "Slower", rate="500k/500k")
    _set_collections_profile(db_session, slow)
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=slow, pre_throttle=ten)

    with pytest.raises(HTTPException) as excinfo:
        _set_collections_profile(db_session, slower)
    assert excinfo.value.status_code == 409
    # Re-asserting the same value is not a change.
    _set_collections_profile(db_session, slow)

    # Once nobody is throttled the change goes through.
    credential.radius_profile_id = None
    credential.pre_throttle_radius_profile_id = None
    db_session.commit()
    _set_collections_profile(db_session, slower)
    assert owner.RateContext.load(db_session).collections_profile_id == slower.id


# ---------------------------------------------------------------- preflight


def test_preflight_lists_every_divergence_class_and_exits_non_zero(
    db_session, monkeypatch, capsys
):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    twelve = _profile(db_session, "Twelve", rate="12M/12M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    fup_like = _profile(
        db_session, "Fup like", rate="3M/3M", code="fup-throttle-3000k-3000k"
    )
    slow = _profile(db_session, "Slow", rate="1M/1M", down=1000, code="coll")
    _set_collections_profile(db_session, slow)

    offer = _offer(db_session, "A", profile=ten)
    ambiguous = _offer(db_session, "Amb", profile=ten)
    db_session.add(OfferRadiusProfile(offer_id=ambiguous.id, profile_id=twelve.id))
    unlinked = _offer(db_session, "Unlinked")
    db_session.commit()

    clean = _service(db_session, offer, sub_profile=ten)[0]
    credential_wins = _service(db_session, offer, sub_profile=ten, slot=fifteen)[0]
    sub_differs = _service(db_session, offer, sub_profile=twelve)[0]
    no_link = _service(db_session, unlinked, sub_profile=fifteen)[0]
    amb = _service(db_session, ambiguous, sub_profile=ten)[0]
    unrecognised = _service(db_session, offer, sub_profile=ten, slot=fup_like)[0]
    not_projected = _service(db_session, offer, sub_profile=ten, pre_throttle=ten)[0]
    projected_throttle = _service(
        db_session, offer, sub_profile=ten, slot=slow, pre_throttle=ten
    )[0]

    rows = {row.subscription_id: row.klass for row in owner.preflight(db_session)}

    assert str(clean.id) not in rows
    assert str(projected_throttle.id) not in rows
    assert rows[str(credential_wins.id)] == "credential_wins"
    assert rows[str(sub_differs.id)] == "subscription_differs"
    assert rows[str(no_link.id)] == "no_package_link"
    assert rows[str(amb.id)] == "ambiguous_link"
    assert rows[str(unrecognised.id)] == "unrecognised_throttle"
    assert rows[str(not_projected.id)] == "throttle_not_projected"
    assert set(rows.values()) == set(owner.DIVERGENCE_CLASSES)

    # CLI: one JSON line per row, a summary, and a non-zero exit.
    _borrow_session(monkeypatch, db_session)
    assert owner.main(["preflight"]) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["divergences"] == len(rows)
    assert owner.main(["other"]) == 2


def test_preflight_is_clean_when_nothing_diverges(db_session, monkeypatch, capsys):
    ten = _profile(db_session, "Ten", rate="10M/10M")
    offer = _offer(db_session, "A", profile=ten)
    _service(db_session, offer, sub_profile=ten)
    _borrow_session(monkeypatch, db_session)

    assert owner.preflight(db_session) == []
    assert owner.main(["preflight"]) == 0


# ------------------------------------------------ remaining readers wired


def _reply_rate(attrs):
    return next(
        (a["value"] for a in attrs if a["attribute"] == "Mikrotik-Rate-Limit"), None
    )


def test_nas_reply_attributes_follow_the_package_when_enabled(db_session, monkeypatch):
    from app.services.connection_type_provisioning import (
        build_radius_reply_attributes,
    )

    ten = _profile(db_session, "Ten", rate="10M/10M")
    fifteen = _profile(db_session, "Fifteen", rate="15M/15M")
    offer = _offer(db_session, "A", profile=ten)
    sub, _ = _service(db_session, offer, sub_profile=fifteen)

    monkeypatch.setattr(owner, "rate_owner_enabled", lambda _db: False)
    assert _reply_rate(build_radius_reply_attributes(db_session, sub)) == "15M/15M"

    monkeypatch.setattr(owner, "rate_owner_enabled", lambda _db: True)
    assert _reply_rate(build_radius_reply_attributes(db_session, sub)) == "10M/10M"


def test_fup_full_speed_and_provisioning_use_the_plan_not_the_throttle(
    db_session, owner_on
):
    from app.services import enforcement

    ten = _profile(db_session, "Ten", rate="10M/10M")
    slow = _profile(db_session, "Slow", rate="1M/1M", down=1000)
    _set_collections_profile(db_session, slow)
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=slow, pre_throttle=ten)

    assert enforcement._resolve_plan_profile(db_session, sub).id == ten.id
    assert (
        enforcement._resolve_effective_profile(db_session, sub, credential).id
        == slow.id
    )


def test_identity_signature_sees_a_derived_profile_change(db_session, monkeypatch):
    from app.services import enforcement

    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    offer = _offer(db_session, "A", profile=ten)
    sub, _ = _service(db_session, offer, sub_profile=ten)

    monkeypatch.setattr(owner, "rate_owner_enabled", lambda _db: True)
    before = enforcement.network_identity_signature(db_session, sub)
    _staged(
        db_session,
        sub,
        twenty,
        requested_at=datetime.now(UTC),
        state=SubscriptionChangeExecutionState.provisioning,
    )
    after = enforcement.network_identity_signature(db_session, sub)

    assert before != after
    assert after[3] == str(twenty.id)


def test_admin_credential_form_refuses_a_non_package_profile(db_session, owner_on):
    from app.services.web_catalog_subscriptions import _upsert_access_credential

    ten = _profile(db_session, "Ten", rate="10M/10M")
    other = _profile(db_session, "Other", rate="3M/3M")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, slot=ten)

    with pytest.raises(DomainError):
        _upsert_access_credential(
            db_session,
            subscriber_id=sub.subscriber_id,
            username=sub.login,
            radius_profile_id=str(other.id),
            subscription_id=sub.id,
        )
    _upsert_access_credential(
        db_session,
        subscriber_id=sub.subscriber_id,
        username=sub.login,
        radius_profile_id=str(ten.id),
        subscription_id=sub.id,
    )
