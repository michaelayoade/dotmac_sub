"""Referral customer surfaces are permanently native after revision 356."""

from __future__ import annotations

import uuid

from app.api import me as me_api
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.referral_native import Referral
from app.models.subscriber import Subscriber
from app.schemas.portal import ReferAFriendRequest
from app.services import control_registry, settings_spec
from app.services.crm_client import CRMClient
from app.tasks.referrals import (
    reconcile_referral_mirror,
    refresh_referral_mirror_for_subscriber,
)


def _program(db, *, enabled: bool = True, amount: str = "2500") -> None:
    rows = {
        "referral_program_enabled": "true" if enabled else "false",
        "referral_reward_amount": amount,
    }
    for key, text in rows.items():
        db.add(
            DomainSetting(
                domain=SettingDomain.subscriber,
                key=key,
                value_type=SettingValueType.boolean
                if key == "referral_program_enabled"
                else SettingValueType.string,
                value_text=text,
                is_active=True,
            )
        )
    db.commit()


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Native",
        last_name="Referrer",
        email=f"native-{uuid.uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def test_referral_crm_controls_and_outbound_writer_are_retired():
    controls = {control.key for control in control_registry.all_controls()}
    setting_keys = {(spec.domain, spec.key) for spec in settings_spec.SETTINGS_SPECS}

    assert "referrals.native_read" not in controls
    assert "referrals.native_write" not in controls
    assert (
        SettingDomain.projects,
        "referrals_native_read_enabled",
    ) not in setting_keys
    assert (
        SettingDomain.projects,
        "referrals_native_write_enabled",
    ) not in setting_keys
    assert not hasattr(CRMClient, "create_portal_referral")


def test_retired_referral_mirror_tasks_are_network_free_tombstones():
    assert reconcile_referral_mirror.run() == {"reconciled": 0}
    assert refresh_referral_mirror_for_subscriber.run("legacy-sub") == {
        "refreshed": False
    }


def test_me_referral_capture_is_native_without_crm_link(db_session):
    _program(db_session)
    subscriber = _subscriber(db_session)
    assert subscriber.splynx_customer_id is None
    principal = {
        "principal_type": "subscriber",
        "subscriber_id": str(subscriber.id),
    }

    result = me_api.my_refer_a_friend(
        ReferAFriendRequest(name="Ada Friend", phone="08031234567"),
        db=db_session,
        principal=principal,
    )

    referral = db_session.get(Referral, uuid.UUID(str(result["id"])))
    assert referral is not None
    assert referral.referred_party_id is not None
    assert referral.referred_subscriber_id is None
    assert result["status"] == "pending"


def test_me_referral_capture_remains_duplicate_guarded(db_session):
    _program(db_session)
    subscriber = _subscriber(db_session)
    principal = {
        "principal_type": "subscriber",
        "subscriber_id": str(subscriber.id),
    }
    payload = ReferAFriendRequest(name="Same Friend", phone="08011112222")

    first = me_api.my_refer_a_friend(payload, db=db_session, principal=principal)
    second = me_api.my_refer_a_friend(payload, db=db_session, principal=principal)

    assert first["id"] == second["id"]
    assert (
        db_session.query(Referral)
        .filter(Referral.referrer_subscriber_id == subscriber.id)
        .count()
        == 1
    )
