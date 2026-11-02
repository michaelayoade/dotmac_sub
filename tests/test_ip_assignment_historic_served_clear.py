"""Behavior tests for clear_historic_served_ipv4 and its restore inverse.

W0 N5 column pass (ruling R7). Addresses use the RFC 5737 documentation range.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.event_store import EventStore
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.models.test_connection import TestConnectionGrant
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services.ip_assignment_lifecycle import (
    ClearedServedIPv4RestoreDecision,
    ClearHistoricServedIPv4Command,
    HistoricServedIPv4ClearDecision,
    IPAssignmentOwnershipError,
    RestoreClearedServedIPv4Command,
    clear_historic_served_ipv4,
    preview_cleared_served_ipv4_restore,
    preview_historic_served_ipv4_clear,
    restore_cleared_served_ipv4,
)
from app.services.owner_commands import CommandContext, OwnerCommandError

D = HistoricServedIPv4ClearDecision
R = ClearedServedIPv4RestoreDecision
HISTORIC = "198.51.100.20"


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="IPAM",
        last_name="HistoricClear",
        email=f"ipam-historic-{uuid4().hex[:10]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _subscription(
    db,
    subscriber,
    offer,
    *,
    status=SubscriptionStatus.disabled,
    served_ip=HISTORIC,
    login="auto",
) -> Subscription:
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=f"ipam-historic-{uuid4().hex[:10]}" if login == "auto" else login,
        ipv4_address=served_ip,
    )
    db.add(subscription)
    db.flush()
    return subscription


def _pool(db) -> IpPool:
    pool = IpPool(
        name=f"Subscriber Pool {uuid4().hex[:6]}",
        ip_version=IPVersion.ipv4,
        cidr="198.51.100.0/24",
        is_active=True,
    )
    db.add(pool)
    db.flush()
    return pool


def _assignment(db, subscriber, pool, value, *, subscription=None, primary=False):
    address = db.scalar(select(IPv4Address).where(IPv4Address.address == value))
    if address is None:
        address = IPv4Address(address=value, pool_id=pool.id, allocation_type="static")
        db.add(address)
        db.flush()
    row = IPAssignment(
        subscriber_id=subscriber.id,
        subscription_id=subscription.id if subscription is not None else None,
        ip_version=IPVersion.ipv4,
        ipv4_address_id=address.id,
        is_active=True,
        is_primary=primary,
    )
    db.add(row)
    db.flush()
    return row


def _historic(db, subscriber, subscription, value="198.51.100.30"):
    """Give the service its own exact assignment on a different address, so the
    served column is genuinely historic (not an N9 link candidate)."""
    return _assignment(
        db, subscriber, _pool(db), value, subscription=subscription, primary=True
    )


def _clear_command(subscription_id: UUID, fingerprint: str, key="ipam-clear-1"):
    return ClearHistoricServedIPv4Command(
        context=CommandContext.system(
            actor="test-operator",
            scope="ipam_historic_served_ipv4",
            reason="Reviewed N5 column pass",
            idempotency_key=key,
        ),
        subscription_id=subscription_id,
        preview_fingerprint=fingerprint,
    )


def _restore_command(subscription_id: UUID, fingerprint: str, key="ipam-restore-1"):
    return RestoreClearedServedIPv4Command(
        context=CommandContext.system(
            actor="test-operator",
            scope="ipam_historic_served_ipv4",
            reason="Reviewed N5 column restore",
            idempotency_key=key,
        ),
        subscription_id=subscription_id,
        preview_fingerprint=fingerprint,
    )


def _preview(db, subscription_id):
    db.commit()
    result = preview_historic_served_ipv4_clear(db, subscription_id=subscription_id)
    db.commit()
    return result


def _restore_preview(db, subscription_id):
    db.commit()
    result = preview_cleared_served_ipv4_restore(db, subscription_id=subscription_id)
    db.commit()
    return result


def _clear(db, command):
    db.commit()  # owner commands need a transaction-free session
    return clear_historic_served_ipv4(db, command)


def _restore(db, command):
    db.commit()
    return restore_cleared_served_ipv4(db, command)


@pytest.fixture()
def world(db_session, catalog_offer):
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    _historic(db_session, subscriber, subscription, "198.51.100.40")
    db_session.add(IPv4Address(address=HISTORIC, pool_id=_pool(db_session).id))
    db_session.commit()
    return db_session, catalog_offer, subscriber, subscription


# --- clear: eligible ---------------------------------------------------------


def test_clear_nulls_only_the_column_and_preserves_everything_else(world) -> None:
    db, offer, subscriber, subscription = world
    pool = _pool(db)
    other = _assignment(
        db, subscriber, pool, "198.51.100.30", subscription=subscription
    )
    unrelated = _assignment(db, subscriber, pool, "198.51.100.31")
    db.commit()
    updated_before = subscription.updated_at
    events_before = db.scalar(select(func.count()).select_from(EventStore))

    preview = _preview(db, subscription.id)
    assert preview.decision is D.ready_clear
    assert (
        db.get(Subscription, subscription.id).ipv4_address == HISTORIC
    )  # preview wrote 0

    outcome = _clear(db, _clear_command(subscription.id, preview.fingerprint))
    assert outcome.cleared_ipv4 == HISTORIC
    assert outcome.replayed is False
    db.refresh(subscription)
    assert subscription.ipv4_address is None
    assert subscription.updated_at == updated_before  # retention evidence intact
    db.refresh(other)
    db.refresh(unrelated)
    assert other.is_active and other.subscription_id == subscription.id
    assert unrelated.is_active and unrelated.subscription_id is None
    # No re-projection event, no RADIUS consequence.
    assert db.scalar(select(func.count()).select_from(EventStore)) == events_before
    actions = set(
        db.scalars(
            select(AuditEvent.action).where(
                AuditEvent.entity_id.in_([str(subscription.id), "ipam-clear-1"])
            )
        )
    )
    assert {
        "network.ip_assignment.historic_served_ipv4_cleared",
        "network.ip_assignment.historic_served_ipv4_cleared_batch",
    } <= actions


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.disabled,
        SubscriptionStatus.hidden,
        SubscriptionStatus.archived,
        SubscriptionStatus.canceled,
        SubscriptionStatus.expired,
    ],
)
def test_clear_ready_for_historic_statuses(db_session, catalog_offer, status) -> None:
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, catalog_offer, status=status)
    _historic(db_session, subscriber, subscription)
    assert _preview(db_session, subscription.id).decision is D.ready_clear


def test_clear_ready_for_non_radius_service_without_provisioning_nas(
    db_session, catalog_offer
) -> None:
    subscriber = _subscriber(db_session)
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.disabled,
        login=None,
    )
    _historic(db_session, subscriber, subscription)
    assert _preview(db_session, subscription.id).decision is D.ready_clear


def test_clear_ready_when_own_primary_differs_from_column(world) -> None:
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    assert preview.decision is D.ready_clear
    assert preview.own_primary_address == "198.51.100.40"
    assert preview.own_exact_addresses == ("198.51.100.40",)


def test_clear_ready_when_column_ip_is_held_by_another_assignment(
    db_session, catalog_offer
) -> None:
    """No own assignment, but the column IP is owned elsewhere in IPAM: the
    column is historic residue that collides, which is what B2 clears."""
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    holder = _subscriber(db_session)
    _assignment(db_session, holder, _pool(db_session), HISTORIC)
    preview = _preview(db_session, subscription.id)
    assert preview.ip_held_by_active_assignment is True
    assert preview.decision is D.ready_clear


# --- clear: refusals ---------------------------------------------------------


def test_clear_refuses_missing_subscription(db_session) -> None:
    assert _preview(db_session, uuid4()).decision is D.subscription_not_found


def test_clear_refuses_empty_column(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, catalog_offer, served_ip=None)
    assert _preview(db_session, subscription.id).decision is D.served_ipv4_empty


def test_clear_refuses_active_service(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session)
    subscription = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )
    assert _preview(db_session, subscription.id).decision is D.subscription_active


def test_clear_refuses_column_equal_to_a_non_primary_own_assignment(world) -> None:
    db, _, subscriber, subscription = world
    _assignment(
        db,
        subscriber,
        _pool(db),
        HISTORIC,
        subscription=subscription,
        primary=False,
    )
    assert _preview(db, subscription.id).decision is D.column_matches_own_assignment


@pytest.mark.parametrize(
    "status", [SubscriptionStatus.suspended, SubscriptionStatus.blocked]
)
def test_clear_refuses_suspended_and_blocked_unconditionally(
    db_session, catalog_offer, status
) -> None:
    """enforcement.py lift/cancel cleanup no-op on an empty column and orphan
    the NAS address-list entry, online or not."""
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, catalog_offer, status=status)
    _historic(db_session, subscriber, subscription)
    assert (
        _preview(db_session, subscription.id).decision is D.status_suspended_or_blocked
    )


def test_clear_refuses_non_terminal_service_without_exact_assignment_ip_free(
    db_session, catalog_offer
) -> None:
    """N9: the column is its only record; route to repair ready_create."""
    subscriber = _subscriber(db_session)
    subscription = _subscription(db_session, subscriber, catalog_offer)
    assert (
        _preview(db_session, subscription.id).decision
        is D.column_without_exact_assignment_ip_free
    )


def test_clear_allows_terminal_service_without_exact_assignment(
    db_session, catalog_offer
) -> None:
    subscriber = _subscriber(db_session)
    subscription = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.canceled
    )
    assert _preview(db_session, subscription.id).decision is D.ready_clear


def test_clear_refuses_column_that_is_a_pending_link_key(world) -> None:
    """N5 order: an unbound row links by this column; P3 must reach 0 first."""
    db, _, subscriber, subscription = world
    _assignment(db, subscriber, _pool(db), HISTORIC)  # unbound, same address
    assert _preview(db, subscription.id).decision is D.column_is_pending_link_key


def test_clear_refuses_online_by_subscription_session(world) -> None:
    db, _, _, subscription = world
    db.add(
        RadiusActiveSession(
            subscription_id=subscription.id,
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address="198.51.100.99",
            session_start=datetime.now(UTC),
        )
    )
    assert _preview(db, subscription.id).decision is D.online_session


def test_clear_refuses_online_by_unattributed_session_on_the_address(world) -> None:
    db, _, _, subscription = world
    db.add(
        RadiusActiveSession(
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address=HISTORIC,
            session_start=datetime.now(UTC),
        )
    )
    assert _preview(db, subscription.id).decision is D.online_session


def test_clear_ignores_another_customers_session_on_the_address(world) -> None:
    db, offer, _, subscription = world
    other_sub = _subscription(
        db, _subscriber(db), offer, status=SubscriptionStatus.active, served_ip=None
    )
    db.add(
        RadiusActiveSession(
            subscription_id=other_sub.id,
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address=HISTORIC,
            session_start=datetime.now(UTC),
        )
    )
    assert _preview(db, subscription.id).decision is D.ready_clear


def test_clear_refuses_open_accounting_session(world) -> None:
    db, _, _, subscription = world
    db.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id=f"ipam-acct-{uuid4().hex[:8]}",
            status_type=AccountingStatus.interim,
            session_start=datetime.now(UTC),
        )
    )
    assert _preview(db, subscription.id).decision is D.open_accounting_session


def test_clear_allows_closed_accounting_session(world) -> None:
    db, _, _, subscription = world
    db.add(
        RadiusAccountingSession(
            subscription_id=subscription.id,
            session_id=f"ipam-acct-{uuid4().hex[:8]}",
            status_type=AccountingStatus.stop,
            session_start=datetime.now(UTC) - timedelta(hours=2),
            session_end=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    assert _preview(db, subscription.id).decision is D.ready_clear


def test_clear_refuses_test_connection_access(world) -> None:
    db, _, subscriber, subscription = world
    actor = SystemUser(
        first_name="T", last_name="U", email=f"t{uuid4().hex[:6]}@example.com"
    )
    db.add(actor)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        TestConnectionGrant(
            subscription_id=subscription.id,
            subscriber_id=subscriber.id,
            actor_id=actor.id,
            actor_label="test",
            command_id=uuid4(),
            activated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
            duration_seconds=1860,
        )
    )
    assert _preview(db, subscription.id).decision is D.test_connection_access


@pytest.mark.parametrize("mode", ["active", "captive"])
def test_clear_refuses_service_radius_still_projects(world, monkeypatch, mode) -> None:
    from types import SimpleNamespace

    from app.services import radius_projection_planner

    db, _, _, subscription = world
    login = subscription.login
    monkeypatch.setattr(
        radius_projection_planner,
        "plan_login_radius_projections",
        lambda *_a, **_k: {login: SimpleNamespace(plan=SimpleNamespace(mode=mode))},
    )
    preview = _preview(db, subscription.id)
    assert preview.radius_mode == mode
    assert preview.decision is D.radius_projects_service


def test_clear_refuses_address_list_block_dependency(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session)
    nas = NasDevice(name=f"ipam-nas-{uuid4().hex[:6]}")
    db_session.add(nas)
    db_session.flush()
    subscription = _subscription(
        db_session,
        subscriber,
        catalog_offer,
        status=SubscriptionStatus.disabled,
        login=None,
    )
    _historic(db_session, subscriber, subscription)
    subscription.provisioning_nas_device_id = nas.id
    assert (
        _preview(db_session, subscription.id).decision
        is D.address_list_block_dependency
    )


# --- clear: apply guards -----------------------------------------------------


def test_apply_refuses_stale_fingerprint(world) -> None:
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    db.add(
        RadiusActiveSession(
            subscription_id=subscription.id,
            username="ipam-live",
            acct_session_id=f"ipam-live-{uuid4().hex[:8]}",
            framed_ip_address="198.51.100.99",
            session_start=datetime.now(UTC),
        )
    )
    db.commit()
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _clear(db, _clear_command(subscription.id, preview.fingerprint))
    assert exc.value.code.endswith("stale_preview")
    db.rollback()
    assert db.get(Subscription, subscription.id).ipv4_address == HISTORIC


def test_apply_refuses_inapplicable_preview(db_session, catalog_offer) -> None:
    subscriber = _subscriber(db_session)
    subscription = _subscription(
        db_session, subscriber, catalog_offer, status=SubscriptionStatus.active
    )
    preview = _preview(db_session, subscription.id)
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _clear(db_session, _clear_command(subscription.id, preview.fingerprint))
    assert exc.value.code.endswith("unsafe_cohort")
    db_session.rollback()
    assert db_session.get(Subscription, subscription.id).ipv4_address == HISTORIC


def test_apply_requires_idempotency_key(world) -> None:
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    with pytest.raises(OwnerCommandError, match="idempotency_key"):
        _clear(db, _clear_command(subscription.id, preview.fingerprint, key="  "))
    db.rollback()
    assert db.get(Subscription, subscription.id).ipv4_address == HISTORIC


def test_replay_is_idempotent_and_conflicting_reuse_refused(world) -> None:
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    first = _clear(db, _clear_command(subscription.id, preview.fingerprint))
    again = _clear(db, _clear_command(subscription.id, preview.fingerprint))
    assert first.replayed is False
    assert again.replayed is True
    assert again.cleared_ipv4 == HISTORIC
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _clear(db, _clear_command(subscription.id, "0" * 64))
    assert exc.value.code.endswith("idempotency_conflict")


# --- restore -----------------------------------------------------------------


def test_restore_round_trip(world) -> None:
    db, _, _, subscription = world
    updated_before = subscription.updated_at
    preview = _preview(db, subscription.id)
    _clear(db, _clear_command(subscription.id, preview.fingerprint))

    restore_preview = _restore_preview(db, subscription.id)
    assert restore_preview.decision is R.ready_restore
    assert restore_preview.restore_ipv4 == HISTORIC
    outcome = _restore(
        db, _restore_command(subscription.id, restore_preview.fingerprint)
    )
    assert outcome.restored_ipv4 == HISTORIC
    assert outcome.replayed is False
    db.refresh(subscription)
    assert subscription.ipv4_address == HISTORIC
    assert subscription.updated_at == updated_before
    # A second restore has nothing left to restore.
    assert _restore_preview(db, subscription.id).decision is R.no_clear_record


def test_restore_replay_is_idempotent(world) -> None:
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    _clear(db, _clear_command(subscription.id, preview.fingerprint))
    restore_preview = _restore_preview(db, subscription.id)
    first = _restore(db, _restore_command(subscription.id, restore_preview.fingerprint))
    again = _restore(db, _restore_command(subscription.id, restore_preview.fingerprint))
    assert first.replayed is False and again.replayed is True


def test_restore_refuses_when_never_cleared(world) -> None:
    db, _, _, subscription = world
    assert _restore_preview(db, subscription.id).decision is R.no_clear_record


def test_restore_refuses_missing_subscription(db_session) -> None:
    assert _restore_preview(db_session, uuid4()).decision is R.subscription_not_found


def test_restore_refuses_when_column_repopulated(world) -> None:
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    _clear(db, _clear_command(subscription.id, preview.fingerprint))
    db.get(Subscription, subscription.id).ipv4_address = "198.51.100.77"
    assert _restore_preview(db, subscription.id).decision is R.served_ipv4_not_empty


def test_restore_refuses_when_address_now_in_live_use(world) -> None:
    db, offer, _, subscription = world
    preview = _preview(db, subscription.id)
    _clear(db, _clear_command(subscription.id, preview.fingerprint))
    other = _subscriber(db)
    _subscription(
        db, other, offer, status=SubscriptionStatus.active, served_ip=HISTORIC
    )
    assert _restore_preview(db, subscription.id).decision is R.address_in_live_use


def test_restore_apply_refuses_stale_fingerprint(world) -> None:
    db, offer, _, subscription = world
    preview = _preview(db, subscription.id)
    _clear(db, _clear_command(subscription.id, preview.fingerprint))
    restore_preview = _restore_preview(db, subscription.id)
    other = _subscriber(db)
    _subscription(
        db, other, offer, status=SubscriptionStatus.active, served_ip=HISTORIC
    )
    db.commit()
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _restore(db, _restore_command(subscription.id, restore_preview.fingerprint))
    assert exc.value.code.endswith("stale_preview")
    db.rollback()
    assert db.get(Subscription, subscription.id).ipv4_address is None


def _cleared(world):
    db, _, _, subscription = world
    preview = _preview(db, subscription.id)
    _clear(db, _clear_command(subscription.id, preview.fingerprint))
    return db, subscription


def test_restore_refuses_when_another_assignment_holds_the_address(world) -> None:
    db, subscription = _cleared(world)
    holder = _subscriber(db)
    _assignment(db, holder, _pool(db), HISTORIC)  # unbound, other subscriber
    assert (
        _restore_preview(db, subscription.id).decision
        is R.address_held_by_other_assignment
    )


def test_restore_refuses_reserved_address(world) -> None:
    db, subscription = _cleared(world)
    db.scalar(
        select(IPv4Address).where(IPv4Address.address == HISTORIC)
    ).is_reserved = True
    assert _restore_preview(db, subscription.id).decision is R.address_not_restorable


def test_restore_refuses_device_host(world) -> None:
    from app.models.network_monitoring import NetworkDevice

    db, subscription = _cleared(world)
    db.add(NetworkDevice(name="dev", mgmt_ip=HISTORIC, is_active=True))
    assert _restore_preview(db, subscription.id).decision is R.address_not_restorable


def test_restore_refuses_routed_block(world) -> None:
    from app.models.network import SubscriberAdditionalRoute

    db, subscription = _cleared(world)
    db.add(
        SubscriberAdditionalRoute(
            subscriber_id=subscription.subscriber_id,
            cidr="198.51.100.16/29",
            prefix_length=29,
            metric=1,
            is_active=True,
            source="manual",
        )
    )
    assert _restore_preview(db, subscription.id).decision is R.address_not_restorable


def _assert_stale_restore(db, subscription, reviewed) -> None:
    with pytest.raises(IPAssignmentOwnershipError) as exc:
        _restore(db, _restore_command(subscription.id, reviewed.fingerprint))
    assert exc.value.code.endswith("stale_preview")
    db.rollback()
    assert db.get(Subscription, subscription.id).ipv4_address is None


def test_restore_refuses_actual_enable_path(world) -> None:
    from app.services.account_lifecycle import enable_subscription
    from app.services.radius_projection_planner import plan_login_radius_projections

    db, _, subscriber, subscription = world
    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    assert reviewed.applicable
    subscriber.billing_enabled = True
    db.flush()
    assert enable_subscription(
        db, str(subscription.id), reason="Resume service", source="test", emit=False
    )
    db.commit()
    assert subscription.status is SubscriptionStatus.active
    assert subscription.ipv4_address is None
    assert (
        plan_login_radius_projections(db, [subscription])[subscription.login].plan.mode
        == "active"
    )
    current = _restore_preview(db, subscription.id)
    assert current.decision is R.service_not_restorable
    _assert_stale_restore(db, subscription, reviewed)


@pytest.mark.parametrize(
    "status",
    [
        SubscriptionStatus.active,
        SubscriptionStatus.suspended,
        SubscriptionStatus.blocked,
        SubscriptionStatus.hidden,
    ],
)
def test_restore_status_drift_cannot_be_approved_by_new_preview(world, status) -> None:
    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    subscription.status = status
    current = _restore_preview(db, subscription.id)
    expected = (
        R.service_evidence_changed
        if status is SubscriptionStatus.hidden
        else R.service_not_restorable
    )
    assert current.decision is expected
    _assert_stale_restore(db, subscription, reviewed)


@pytest.mark.parametrize("change", ["primary", "replace", "add", "remove", "address"])
def test_restore_own_assignment_drift_cannot_be_approved(world, change) -> None:
    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    assignment = db.scalar(
        select(IPAssignment).where(IPAssignment.subscription_id == subscription.id)
    )
    if change == "primary":
        assignment.is_primary = False
    elif change == "replace":
        assignment.is_active = False
        db.flush()
        db.add(
            IPAssignment(
                subscriber_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                ip_version=IPVersion.ipv4,
                ipv4_address_id=assignment.ipv4_address_id,
                is_active=True,
                is_primary=True,
            )
        )
    elif change == "add":
        _assignment(
            db,
            db.get(Subscriber, subscription.subscriber_id),
            _pool(db),
            "198.51.100.41",
            subscription=subscription,
        )
    elif change == "remove":
        assignment.is_active = False
    else:
        db.get(IPv4Address, assignment.ipv4_address_id).address = "198.51.100.41"
    assert _restore_preview(db, subscription.id).decision is R.service_evidence_changed
    _assert_stale_restore(db, subscription, reviewed)


@pytest.mark.parametrize(
    "kind", ["own_online", "unattributed_online", "accounting", "login", "nas"]
)
def test_restore_service_dependency_drift(world, kind) -> None:
    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    if kind in {"own_online", "unattributed_online"}:
        db.add(
            RadiusActiveSession(
                subscription_id=subscription.id if kind == "own_online" else None,
                username=subscription.login,
                acct_session_id=f"restore-{uuid4().hex[:8]}",
                framed_ip_address="198.51.100.40" if kind == "own_online" else HISTORIC,
                session_start=datetime.now(UTC),
            )
        )
    elif kind == "accounting":
        db.add(
            RadiusAccountingSession(
                subscription_id=subscription.id,
                session_id=f"restore-{uuid4().hex[:8]}",
                status_type=AccountingStatus.interim,
                session_start=datetime.now(UTC),
            )
        )
    elif kind == "login":
        subscription.login = f"changed-{uuid4().hex[:8]}"
    else:
        nas = NasDevice(name=f"restore-{uuid4().hex[:8]}")
        db.add(nas)
        db.flush()
        subscription.provisioning_nas_device_id = nas.id
    current = _restore_preview(db, subscription.id)
    # A session on the historic address takes the address-live-use refusal;
    # own delivery on another address and accounting take the service refusal.
    expected = {
        "own_online": R.service_not_restorable,
        "unattributed_online": R.address_in_live_use,
        "accounting": R.service_not_restorable,
        "login": R.service_evidence_changed,
        "nas": R.service_evidence_changed,
    }[kind]
    assert current.decision is expected
    assert not current.applicable
    _assert_stale_restore(db, subscription, reviewed)


@pytest.mark.parametrize("mode", ["active", "captive"])
def test_restore_radius_projection_drift(world, monkeypatch, mode) -> None:
    from dataclasses import replace

    from app.services import radius_projection_planner

    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    original = radius_projection_planner.plan_login_radius_projections

    def projecting(db, subscriptions, **kwargs):
        return {
            login: replace(projection, plan=replace(projection.plan, mode=mode))
            for login, projection in original(db, subscriptions, **kwargs).items()
        }

    monkeypatch.setattr(
        radius_projection_planner, "plan_login_radius_projections", projecting
    )
    assert _restore_preview(db, subscription.id).decision is R.service_not_restorable
    _assert_stale_restore(db, subscription, reviewed)


def test_restore_refuses_missing_address_inventory(world) -> None:
    db, subscription = _cleared(world)
    address = db.scalar(select(IPv4Address).where(IPv4Address.address == HISTORIC))
    db.delete(address)
    assert _restore_preview(db, subscription.id).decision is R.address_not_restorable


def test_restore_refuses_legacy_clear_without_service_evidence(world) -> None:
    db, subscription = _cleared(world)
    record = db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "network.ip_assignment.historic_served_ipv4_cleared",
            AuditEvent.entity_id == str(subscription.id),
        )
    )
    record.metadata_ = {
        key: value
        for key, value in record.metadata_.items()
        if key != "restore_service_fingerprint"
    }
    assert _restore_preview(db, subscription.id).decision is R.service_evidence_changed


def test_restore_refuses_new_test_access(world) -> None:
    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    actor = SystemUser(
        first_name="T", last_name="U", email=f"restore-{uuid4().hex}@example.com"
    )
    db.add(actor)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        TestConnectionGrant(
            subscription_id=subscription.id,
            subscriber_id=subscription.subscriber_id,
            actor_id=actor.id,
            actor_label="test",
            command_id=uuid4(),
            activated_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=30),
            duration_seconds=1860,
        )
    )
    assert _restore_preview(db, subscription.id).decision is R.service_not_restorable
    _assert_stale_restore(db, subscription, reviewed)


def test_restore_refuses_new_non_radius_provisioning_dependency(world) -> None:
    db, _, _, subscription = world
    subscription.login = None
    db, subscription = _cleared(world)
    reviewed = _restore_preview(db, subscription.id)
    nas = NasDevice(name=f"restore-{uuid4().hex[:8]}")
    db.add(nas)
    db.flush()
    subscription.provisioning_nas_device_id = nas.id
    assert _restore_preview(db, subscription.id).decision is R.service_not_restorable
    _assert_stale_restore(db, subscription, reviewed)
