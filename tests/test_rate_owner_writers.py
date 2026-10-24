"""One test per retired writer of the credential rate fields (W1-W11).

After the single rate owner, nothing writes a plan rate, an override or a FUP
throttle into ``access_credentials.radius_profile_id``. Each test drives the
real entry point of a former writer and asserts it left a throttled credential
exactly as it found it, and that the owner still derives the right rate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.catalog import SubscriptionStatus
from app.models.fup_state import FupActionStatus, FupState
from app.models.test_connection import TestConnectionGrant
from app.schemas.catalog import (
    AccessCredentialCreate,
    AccessCredentialUpdate,
    SubscriptionUpdate,
)
from app.services import access_rate_profile as owner
from app.services import radius_population
from app.services.catalog.credentials import AccessCredentials
from app.services.catalog.subscriptions import Subscriptions
from app.services.domain_errors import DomainError
from tests.test_access_rate_profile import (
    _offer,
    _profile,
    _radius_env,  # noqa: F401  (fixture)
    _service,
    _set_collections_profile,
    _sweep_rates,
)


@pytest.fixture()
def throttled(db_session):
    """A service throttled by collections (slot = configured profile + marker)."""
    ten = _profile(db_session, "Ten", rate="10M/10M")
    slow = _profile(db_session, "Slow", rate="1M/1M", down=1000, code="coll")
    _set_collections_profile(db_session, slow)
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(
        db_session, offer, sub_profile=ten, slot=slow, pre_throttle=ten
    )
    return sub, credential, ten, slow, offer


def _unchanged(db, credential, slow, ten):
    db.refresh(credential)
    assert credential.radius_profile_id == slow.id
    assert credential.pre_throttle_radius_profile_id == ten.id


def test_w1_plan_change_does_not_lift_a_throttle(db_session, throttled):
    sub, credential, ten, slow, _ = throttled
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    other = _offer(db_session, "B", profile=twenty)
    sub.status = SubscriptionStatus.pending  # no prepaid plan-change preview needed
    db_session.commit()

    with patch("app.services.catalog.subscriptions._sync_credentials_to_radius"):
        Subscriptions.update(
            db_session, str(sub.id), SubscriptionUpdate(offer_id=other.id)
        )

    _unchanged(db_session, credential, slow, ten)
    db_session.refresh(sub)
    assert sub.radius_profile_id == twenty.id
    decision = owner.resolve_effective_rate(db_session, sub, credential)
    assert decision.layer is owner.RateLayer.collections_throttle


def test_w2_editing_an_offers_profile_does_not_lift_a_throttle(db_session, throttled):
    from app.services.web_catalog_offers import (
        sync_offer_radius_profile_to_subscriptions,
    )

    sub, credential, ten, slow, offer = throttled
    twelve = _profile(db_session, "Twelve", rate="12M/12M")

    with patch("app.services.web_catalog_offers.reconcile_subscription_connectivity"):
        sync_offer_radius_profile_to_subscriptions(
            db_session,
            offer_id=str(offer.id),
            previous_profile_id=str(ten.id),
            new_profile_id=str(twelve.id),
        )

    _unchanged(db_session, credential, slow, ten)


def test_w5_fup_throttle_writes_no_credential_and_the_owner_applies_it(db_session):
    from app.services.enforcement import subscription_credential_scope

    ten = _profile(db_session, "Ten", rate="10M/10M")
    fup = _profile(db_session, "Fup", rate="2M/2M", down=2000, code="fup")
    offer = _offer(db_session, "A", profile=ten)
    sub, credential = _service(db_session, offer, sub_profile=ten)
    db_session.add(
        FupState(
            subscription_id=sub.id,
            offer_id=offer.id,
            action_status=FupActionStatus.throttled,
            throttle_profile_id=fup.id,
        )
    )
    db_session.commit()

    subscription_credential_scope(db_session, str(sub.id))

    db_session.refresh(credential)
    assert credential.radius_profile_id is None
    assert (
        owner.resolve_effective_rate(db_session, sub, credential).rate_limit == "2M/2M"
    )


def test_w6_an_admin_subscription_edit_does_not_lift_a_throttle(db_session, throttled):
    from tests.test_pppoe_credential_bridge import _edit

    sub, credential, ten, slow, _ = throttled
    credential.subscription_id = sub.id
    sub.status = SubscriptionStatus.active
    sub.start_at = datetime.now(UTC) - timedelta(days=3)
    sub.next_billing_at = datetime.now(UTC) + timedelta(days=27)
    db_session.commit()

    with (
        patch(
            "app.services.radius_population.reconcile_usernames",
            return_value={"projection_complete": True, "projection_targets": 1},
        ),
        patch(
            "app.services.enforcement.disconnect_subscription_sessions", return_value=0
        ),
        patch(
            "app.services.web_catalog_subscriptions."
            "_reconcile_active_subscription_after_credential_sync"
        ),
    ):
        _edit(db_session, sub, {"login": f"renamed-{uuid.uuid4().hex[:6]}"})

    _unchanged(db_session, credential, slow, ten)


def test_w7_the_admin_credential_form_stores_no_profile(db_session, throttled):
    from app.services.web_catalog_subscriptions import _upsert_access_credential

    sub, credential, ten, slow, _ = throttled

    _upsert_access_credential(
        db_session,
        subscriber_id=sub.subscriber_id,
        username=sub.login,
        subscription_id=sub.id,
    )

    _unchanged(db_session, credential, slow, ten)


def test_w8_the_api_accepts_the_package_profile_but_never_stores_a_profile(
    db_session, throttled
):
    sub, credential, ten, slow, _ = throttled

    created = AccessCredentials.create(
        db_session,
        AccessCredentialCreate(
            subscriber_id=sub.subscriber_id,
            subscription_id=sub.id,
            username=f"api-{uuid.uuid4().hex[:6]}",
            radius_profile_id=ten.id,  # the package's own profile: accepted...
        ),
    )
    assert created.radius_profile_id is None  # ...and never stored

    AccessCredentials.update(
        db_session,
        str(credential.id),
        AccessCredentialUpdate(radius_profile_id=ten.id),
    )
    _unchanged(db_session, credential, slow, ten)

    other = _profile(db_session, "Other", rate="3M/3M")
    with pytest.raises(DomainError):
        AccessCredentials.update(
            db_session,
            str(credential.id),
            AccessCredentialUpdate(radius_profile_id=other.id),
        )


def test_w9_ensuring_the_pppoe_credential_does_not_lift_a_throttle(
    db_session, throttled
):
    from app.services.pppoe_credentials import (
        EnsurePppoeCredentialCommand,
        ensure_pppoe_credential,
    )

    sub, credential, ten, slow, _ = throttled
    credential.subscription_id = sub.id
    db_session.commit()

    ensure_pppoe_credential(
        db_session,
        EnsurePppoeCredentialCommand(
            subscriber_id=sub.subscriber_id, subscription_id=sub.id
        ),
    )

    _unchanged(db_session, credential, slow, ten)


def test_w10_binding_a_credential_keeps_the_throttle(db_session, throttled):
    from app.services import access_credential_binding as binding

    sub, credential, ten, slow, _ = throttled
    credential.subscription_id = None
    db_session.commit()

    with patch.object(binding, "owner_command_active", lambda *_a, **_k: True):
        binding.stage_access_credential_binding(
            db_session,
            binding.BindAccessCredentialCommand(
                credential_id=credential.id,
                subscriber_id=sub.subscriber_id,
                target_subscription_id=sub.id,
                target_radius_profile_id=ten.id,
                actor="test",
            ),
        )

    _unchanged(db_session, credential, slow, ten)
    assert credential.subscription_id == sub.id


def test_w11_remote_plan_change_stages_on_the_request_not_the_credential(db_session):
    from datetime import date

    from app.models.subscription_change import (
        SubscriptionChangeExecutionState,
        SubscriptionChangeRequest,
    )
    from app.services.subscription_change_execution import stage_remote_reprovision

    ten = _profile(db_session, "Ten", rate="10M/10M")
    twenty = _profile(db_session, "Twenty", rate="20M/20M")
    offer_a = _offer(db_session, "A", profile=ten)
    offer_b = _offer(db_session, "B", profile=twenty)
    sub, credential = _service(db_session, offer_a, sub_profile=ten)
    request = SubscriptionChangeRequest(
        subscription_id=sub.id,
        current_offer_id=offer_a.id,
        requested_offer_id=offer_b.id,
        effective_date=date(2026, 1, 1),
        execution_state=SubscriptionChangeExecutionState.provisioning,
        remote_radius_profile_id=twenty.id,
    )
    db_session.add(request)
    db_session.commit()

    stage_remote_reprovision(db_session, request)
    db_session.commit()

    db_session.refresh(credential)
    assert credential.radius_profile_id is None
    staged = owner.resolve_effective_rate(db_session, sub, credential)
    assert staged.layer is owner.RateLayer.plan_change_staged
    assert staged.rate_limit == "20M/20M"


@pytest.mark.usefixtures("_radius_env")
def test_sweep_with_an_active_test_grant_under_a_throttle(
    db_session, throttled, monkeypatch
):
    """Test access keeps the plan rate; the normal rows stay throttled."""
    from app.models.system_user import SystemUser

    sub, credential, ten, slow, _ = throttled
    login = sub.login
    actor = SystemUser(
        first_name="T", last_name="U", email=f"t{uuid.uuid4().hex[:6]}@example.com"
    )
    db_session.add(actor)
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add(
        TestConnectionGrant(
            subscription_id=sub.id,
            subscriber_id=sub.subscriber_id,
            actor_id=actor.id,
            actor_label="test",
            command_id=uuid.uuid4(),
            activated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            duration_seconds=3660,
        )
    )
    db_session.commit()

    rates = _sweep_rates(monkeypatch)

    normal, test_rate, _group = rates[login]
    assert normal == "1M/1M"  # the collections throttle shapes the normal rows
    assert test_rate == "10M/10M"  # test access always gets the plan rate
    assert radius_population  # imported for the monkeypatched sweep helpers
