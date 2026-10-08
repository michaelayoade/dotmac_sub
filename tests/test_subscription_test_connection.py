"""Troubleshooting access is bounded and independent of commercial eligibility."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.catalog import (
    AccessCredential,
    AccessState,
    BillingMode,
    SubscriptionStatus,
)
from app.models.durable_timer import DurableTimer
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.system_user import SystemUser
from app.models.test_connection import TestConnectionGrant as ConnectionGrant
from app.services import test_connection as owner
from app.services.access_resolution import resolve_customer_access
from app.services.owner_commands import CommandContext
from app.services.test_connection_policy import (
    ObservedRadiusAttribute,
    effective_radius_observation,
)
from app.services.test_connection_policy import TestConnectionAccess as ConnectionAccess


@pytest.fixture
def test_service(db_session, subscriber, subscription, monkeypatch):
    staff = SystemUser(
        first_name="Ada",
        last_name="Engineer",
        email=f"test-{uuid4()}@example.com",
        is_active=True,
    )
    subscription.login = f"test-{uuid4()}"
    subscription.status = SubscriptionStatus.suspended
    subscriber.billing_enabled = False
    subscriber.is_active = False
    credential = AccessCredential(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id,
        username=subscription.login,
        secret_hash="fixture-secret",
        is_active=False,
    )
    db_session.add_all([staff, credential])
    db_session.flush()
    actor_id, account_id, subscription_id = staff.id, subscriber.id, subscription.id
    db_session.commit()
    monkeypatch.setattr(
        owner,
        "configuration",
        lambda db: owner.TestConnectionConfiguration(
            default_hours=2, maximum_hours=24, deadline_verified=True
        ),
    )
    monkeypatch.setattr(
        "app.services.external_radius_targets.active_external_radius_targets",
        lambda db, **kwargs: [{"db_url": "postgresql://test@localhost/test_radius"}],
    )
    monkeypatch.setattr(
        "app.services.credential_crypto.decrypt_credential",
        lambda value: "fixture-password",
    )
    # Keep the real transactional outbox but do not perform network delivery in
    # owner-record unit tests. Transport behavior has separate tests.
    from app.services.events import dispatcher

    emit = dispatcher.emit_event
    monkeypatch.setattr(
        owner,
        "emit_event",
        lambda db, event_type, payload, **kwargs: emit(
            db, event_type, payload, dispatch_after_commit=False, **kwargs
        ),
    )
    command_id = uuid4()
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=str(actor_id),
        scope=owner.PERMISSION,
        reason="Customer connectivity troubleshooting",
        idempotency_key=str(command_id),
    )
    return owner.ActivateTestConnectionCommand(
        context=context,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        actor_id=actor_id,
    )


def test_activation_stages_grant_audit_and_required_timer_atomically(
    db_session, test_service
):
    outcome = owner.activate_test_connection(db_session, command=test_service)
    assert outcome.duration_seconds == 7200
    assert outcome.expires_at - outcome.activated_at == timedelta(hours=2)
    grant = db_session.get(ConnectionGrant, outcome.grant_id)
    timer = db_session.scalar(
        select(DurableTimer).where(DurableTimer.entity_id == grant.id)
    )
    audit = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "subscription.test_connection_activated"
        )
    )
    assert timer.output_event_type == owner.EXPIRY_TRIGGER
    assert timer.due_at.replace(tzinfo=UTC) == outcome.expires_at
    assert audit.actor_id == str(test_service.actor_id)
    assert audit.metadata_["duration_seconds"] == 7200
    assert audit.metadata_["expires_at"] == outcome.expires_at.isoformat()
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    assert subscription.status is SubscriptionStatus.suspended
    assert subscription.subscriber.billing_enabled is False


def test_request_replay_never_extends_the_grant(db_session, test_service):
    first = owner.activate_test_connection(db_session, command=test_service)
    second = owner.activate_test_connection(db_session, command=test_service)
    assert second.replayed
    assert second.grant_id == first.grant_id
    assert second.expires_at == first.expires_at


def test_overlapping_activation_is_refused(db_session, test_service):
    owner.activate_test_connection(db_session, command=test_service)
    context = replace(test_service.context, command_id=uuid4())
    with pytest.raises(owner.TestConnectionError, match="already running"):
        owner.activate_test_connection(
            db_session, command=replace(test_service, context=context)
        )


@pytest.mark.parametrize("hours", [1, 2, 3, 4, 12, 24])
def test_configured_duration_is_snapshotted(
    db_session, test_service, hours, monkeypatch
):
    monkeypatch.setattr(
        owner,
        "configuration",
        lambda db: owner.TestConnectionConfiguration(hours, 24, True),
    )
    result = owner.activate_test_connection(db_session, command=test_service)
    assert result.expires_at - result.activated_at == timedelta(hours=hours)


@pytest.mark.parametrize("hours", [0, -1, 25])
def test_configured_duration_bounds_are_enforced(
    db_session, test_service, hours, monkeypatch
):
    monkeypatch.setattr(
        owner,
        "configuration",
        lambda db: owner.TestConnectionConfiguration(hours, 24, True),
    )
    with pytest.raises(owner.TestConnectionError, match="between 1 and 24"):
        owner.activate_test_connection(db_session, command=test_service)


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.blocked,
        SubscriptionStatus.suspended,
        SubscriptionStatus.disabled,
        SubscriptionStatus.expired,
        SubscriptionStatus.pending,
    ],
)
def test_billing_status_cannot_deny_a_provisioned_test(
    db_session, test_service, status
):
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    subscription.status = status
    subscription.billing_mode = BillingMode.prepaid
    db_session.commit()
    result = owner.activate_test_connection(db_session, command=test_service)
    access = owner.access_for_subscription(db_session, test_service.subscription_id)
    decision = resolve_customer_access(subscription, test_access=access)
    assert decision.radius_access_state is AccessState.active
    assert not decision.state.postpaid_invoice_eligible
    assert decision.state.subscription_status == status.value
    assert result.duration_seconds == 7200


def test_out_of_scope_subscription_and_permission_are_rejected(
    db_session, test_service
):
    with pytest.raises(owner.TestConnectionError, match="does not belong"):
        owner.activate_test_connection(
            db_session, command=replace(test_service, subscriber_id=uuid4())
        )
    with pytest.raises(owner.TestConnectionError, match="authorized staff"):
        owner.activate_test_connection(
            db_session,
            command=replace(
                test_service,
                context=replace(test_service.context, scope="subscription:read"),
            ),
        )


def test_security_hold_is_not_a_billing_bypass(db_session, test_service):
    db_session.add(
        EnforcementLock(
            subscription_id=test_service.subscription_id,
            subscriber_id=test_service.subscriber_id,
            reason=EnforcementReason.fraud,
            source="fraud-review",
            is_active=True,
        )
    )
    db_session.commit()
    with pytest.raises(owner.TestConnectionError, match="fraud/security"):
        owner.activate_test_connection(db_session, command=test_service)


def test_timer_failure_rolls_back_the_activation(db_session, test_service, monkeypatch):
    def fail_timer(*args, **kwargs):
        raise RuntimeError("timer staging failed")

    monkeypatch.setattr(owner, "schedule_timer", fail_timer)
    with pytest.raises(RuntimeError, match="timer staging"):
        owner.activate_test_connection(db_session, command=test_service)
    assert (
        db_session.scalar(
            select(ConnectionGrant.id).where(
                ConnectionGrant.subscription_id == test_service.subscription_id
            )
        )
        is None
    )
    assert (
        db_session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == "subscription.test_connection_activated"
            )
        )
        is None
    )


def test_expiry_recomputes_normal_state_instead_of_restoring_a_snapshot(
    db_session, test_service
):
    result = owner.activate_test_connection(db_session, command=test_service)
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    subscription.status = SubscriptionStatus.active
    subscription.subscriber.is_active = True
    from app.models.subscriber import SubscriberStatus

    subscription.subscriber.status = SubscriberStatus.active
    grant = db_session.get(ConnectionGrant, result.grant_id)
    grant.activated_at = datetime.now(UTC) - timedelta(hours=3)
    grant.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.commit()
    owner.expire_test_connection(
        db_session,
        command=owner.ExpireTestConnectionCommand(
            context=CommandContext.system(
                actor=owner.OWNER, scope=owner.OWNER, reason="expiry"
            ),
            grant_id=result.grant_id,
        ),
    )
    assert owner.access_for_subscription(db_session, subscription.id) is None
    assert (
        resolve_customer_access(subscription).radius_access_state is AccessState.active
    )


def test_deadline_is_enforced_before_timer_delivery(db_session, test_service):
    result = owner.activate_test_connection(db_session, command=test_service)
    query = owner.TestConnectionQuery(
        subscription_ids=(test_service.subscription_id,), evaluated_at=result.expires_at
    )
    assert owner.current_access(db_session, query=query) == ()


def test_observed_projection_switches_to_current_normal_rows_at_deadline():
    now = datetime.now(UTC)
    checks = (
        ObservedRadiusAttribute("login", "Auth-Type", "Reject"),
        ObservedRadiusAttribute(
            "login",
            "Dotmac-Test-Until",
            str(int((now + timedelta(minutes=5)).timestamp())),
        ),
        ObservedRadiusAttribute("login", "Dotmac-Test-Cleartext-Password", "secret"),
    )
    before = effective_radius_observation(
        checks=checks, replies=(), evaluated_at=now, suspended_list="suspended"
    )
    after = effective_radius_observation(
        checks=checks,
        replies=(),
        evaluated_at=now + timedelta(minutes=6),
        suspended_list="suspended",
    )
    assert before.rejected == frozenset()
    assert after.rejected == {"login"}


def test_remaining_duration_does_not_restart_on_reauthentication():
    now = datetime.now(UTC)
    access = ConnectionAccess(
        grant_id=uuid4(),
        subscription_id=uuid4(),
        activated_at=now,
        expires_at=now + timedelta(hours=2),
    )
    assert access.remaining_seconds(now + timedelta(minutes=115)) == 300
    assert not access.valid_at(access.expires_at)


def test_test_connection_button_follows_invoice_outside_its_active_condition():
    template = Path("templates/admin/customers/detail.html").read_text(encoding="utf-8")
    invoice = template.index('title="Generate Invoice"')
    test = template.index("Test Connection</a>", invoice)
    assert "{% endif %}" in template[invoice:test]
    assert "{% if can_test_connection %}" in template[invoice:test]


def test_test_connection_editor_uses_the_system_duration_only():
    template = Path("templates/admin/customers/test_connection.html").read_text(
        encoding="utf-8"
    )
    assert "Configured test duration" in template
    assert 'name="duration_hours"' not in template
    assert "Update duration" not in template


def test_timeline_records_staff_and_the_complete_granted_interval(
    db_session, test_service
):
    from app.services.customer_timeline import build_customer_timeline

    result = owner.activate_test_connection(db_session, command=test_service)
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    timeline = build_customer_timeline(
        db_session,
        customer_id=str(test_service.subscriber_id),
        account_ids=[test_service.subscriber_id],
        subscriptions=[subscription],
    )
    entry = next(
        item for item in timeline if item["title"] == "Test Connection activated"
    )
    assert entry["actor_label"] == "Ada Engineer"
    details = {item["label"]: item["value"] for item in entry["details"]}
    assert details["Duration granted"] == "2 hour(s)"
    assert details["Activated at"] == result.activated_at.isoformat()
    assert details["Expected expiry"] == result.expires_at.isoformat()


def test_late_financial_disconnect_and_address_block_cannot_interrupt_test(
    db_session, test_service, monkeypatch
):
    from app.services import enforcement

    owner.activate_test_connection(db_session, command=test_service)
    monkeypatch.setattr(enforcement, "_address_list_block_enabled", lambda db: True)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "A billing consequence must not reach the live network during a test"
        )

    monkeypatch.setattr(enforcement, "_open_radacct_sessions_for_username", forbidden)
    monkeypatch.setattr(enforcement, "_enforce_address_list_on_nas", forbidden)
    assert (
        enforcement.disconnect_subscription_sessions(
            db_session,
            str(test_service.subscription_id),
            reason="late_financial_suspension",
        )
        == 0
    )
    assert (
        enforcement.apply_subscription_address_list_block(
            db_session, str(test_service.subscription_id)
        )
        == 0
    )


def test_profile_update_reauthenticates_with_remaining_test_access(
    db_session, test_service, monkeypatch
):
    from app.services import enforcement

    owner.activate_test_connection(db_session, command=test_service)
    calls = []

    def disconnect(db, subscription_id, **kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(enforcement, "disconnect_subscription_sessions", disconnect)
    assert (
        enforcement.update_subscription_sessions(
            db_session, str(test_service.subscription_id), reason="fup_throttle"
        )
        == 1
    )
    assert calls[0]["refresh_test_access"] is True
    assert calls[0]["require_terminal"] is True


def test_radius_planner_keeps_normal_financial_decision_under_the_grant(
    db_session, test_service
):
    from app.services.radius_projection_planner import plan_login_radius_projections

    owner.activate_test_connection(db_session, command=test_service)
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    effective = plan_login_radius_projections(db_session, [subscription])[
        subscription.login
    ].plan
    normal = plan_login_radius_projections(
        db_session, [subscription], include_test_access=False
    )[subscription.login].plan
    assert effective.mode == "active"
    assert effective.test_access is not None
    assert normal.mode == "reject"
    assert normal.test_access is None
    assert subscription.status is SubscriptionStatus.suspended


def test_dual_projection_preserves_normal_reject_and_full_plan_override():
    from app.services.radius_population import (
        RadiusProjectionWorkItem,
        _projection_rows_for_item,
    )

    now = datetime.now(UTC)
    access = ConnectionAccess(
        grant_id=uuid4(),
        subscription_id=uuid4(),
        activated_at=now,
        expires_at=now + timedelta(hours=2),
    )
    item = RadiusProjectionWorkItem(
        username="login",
        cleartext_password="fixture-password",
        check_attrs=(),
        reply_attrs=(),
        blocked=True,
        status=SubscriptionStatus.suspended,
        mode="reject",
        profile_group="throttled",
        test_access=access,
        test_reply_attrs=(("Mikrotik-Rate-Limit", ":=", "100M/100M"),),
        test_check_attrs=(("Simultaneous-Use", ":=", "1"),),
        test_profile_group="full-plan",
    )
    checks, replies, groups = _projection_rows_for_item(
        item,
        {
            "password_attribute": "Cleartext-Password",
            "password_op": ":=",
            "default_reply_op": ":=",
            "use_group": True,
            "group_priority": 1,
        },
        access_groups={"active": "dotmac-active", "suspended": "dotmac-suspended"},
        access_group_priority=10,
        group_routing_enabled=True,
    )
    assert ("Auth-Type", "Reject") in {
        (row["attribute"], row["value"]) for row in checks
    }
    assert ("Dotmac-Test-Until", str(int(access.expires_at.timestamp()))) in {
        (row["attribute"], row["value"]) for row in checks
    }
    assert {row["attribute"] for row in replies} == {"Dotmac-Test-Mikrotik-Rate-Limit"}
    assert {row["groupname"] for row in groups} == {
        "dotmac-suspended",
        "Dotmac-Test-full-plan",
        "Dotmac-Test-dotmac-active",
    }


def test_test_profile_restores_routes_without_captive_billing_restrictions():
    from app.models.catalog import CatalogOffer, RadiusProfile
    from app.services.radius_population import _radreply_attrs

    sub = owner.Subscription(
        id=uuid4(),
        subscriber_id=uuid4(),
        offer_id=uuid4(),
        status=SubscriptionStatus.suspended,
        ipv4_address="10.0.0.5",
    )
    profile = RadiusProfile(
        name="full-plan", mikrotik_rate_limit="100M/100M", simultaneous_use=1
    )
    offer = CatalogOffer(name="full-plan")
    attrs = _radreply_attrs(
        sub,
        offer,
        profile,
        subscriber_blocked=True,
        captive_redirect_enabled=True,
        full_test_access=True,
        additional_routes=[("203.0.113.0/29", 1)],
        delegated_ipv6="2001:db8::/56",
        simultaneous_use_enabled=True,
    )
    assert ("Mikrotik-Rate-Limit", ":=", "100M/100M") in attrs
    assert ("Framed-Route", "+=", "203.0.113.0/29 0.0.0.0 1") in attrs
    assert ("Delegated-IPv6-Prefix", ":=", "2001:db8::/56") in attrs
    assert not any(attribute == "Mikrotik-Address-List" for attribute, _, _ in attrs)


def test_new_security_hold_invalidates_current_test_evidence(db_session, test_service):
    owner.activate_test_connection(db_session, command=test_service)
    db_session.add(
        EnforcementLock(
            subscription_id=test_service.subscription_id,
            subscriber_id=test_service.subscriber_id,
            reason=EnforcementReason.fraud,
            source="fraud-review",
            is_active=True,
        )
    )
    db_session.commit()
    assert (
        owner.access_for_subscription(db_session, test_service.subscription_id) is None
    )


def test_network_capability_must_be_verified_before_activation(
    db_session, test_service, monkeypatch
):
    monkeypatch.setattr(
        owner,
        "configuration",
        lambda db: owner.TestConnectionConfiguration(2, 24, False),
    )
    with pytest.raises(owner.TestConnectionError, match="must be verified"):
        owner.activate_test_connection(db_session, command=test_service)


def test_preview_defaults_to_two_hours_and_shows_expected_expiry(
    db_session, test_service
):
    preview = owner.preview_test_connection(
        db_session,
        query=owner.TestConnectionPreviewQuery(
            subscriber_id=test_service.subscriber_id,
            subscription_id=test_service.subscription_id,
        ),
    )
    assert preview.duration_hours == 2
    assert preview.can_activate
    assert 7190 <= (preview.expected_expiry - datetime.now(UTC)).total_seconds() <= 7200


def test_obsolete_shared_login_does_not_block_selected_subscription(
    db_session, test_service
):
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    db_session.add(
        owner.Subscription(
            subscriber_id=subscription.subscriber_id,
            offer_id=subscription.offer_id,
            login=subscription.login,
            status=SubscriptionStatus.expired,
        )
    )
    db_session.commit()
    result = owner.activate_test_connection(db_session, command=test_service)
    assert result.duration_seconds == 7200


def test_live_shared_login_without_subscription_ownership_is_refused(
    db_session, test_service
):
    subscription = db_session.get(owner.Subscription, test_service.subscription_id)
    credential = db_session.scalar(
        select(AccessCredential).where(
            AccessCredential.subscription_id == subscription.id
        )
    )
    credential.subscription_id = None
    db_session.add(
        owner.Subscription(
            subscriber_id=subscription.subscriber_id,
            offer_id=subscription.offer_id,
            login=subscription.login,
            status=SubscriptionStatus.active,
        )
    )
    db_session.commit()
    with pytest.raises(owner.TestConnectionError, match="shared by multiple live"):
        owner.activate_test_connection(db_session, command=test_service)
