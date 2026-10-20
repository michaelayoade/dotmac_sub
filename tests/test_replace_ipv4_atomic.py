"""Replace IPv4 is atomic: IPAM move + served projection, or neither."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.models.catalog import SubscriptionStatus
from app.models.network import IPAssignment, IpBlock, IpPool, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.services import ip_assignment_lifecycle as lifecycle
from app.services import web_catalog_subscriptions as web_service

LOGIN = "atomic-user"


def _setup(db_session, subscription, *, served="10.93.0.4", ipam="10.93.0.4"):
    subscription.status = SubscriptionStatus.active
    subscription.login = LOGIN
    subscription.ipv4_address = served
    pool = IpPool(
        name=f"Atomic {uuid4()}",
        ip_version=IPVersion.ipv4,
        cidr="10.93.0.0/29",
        is_active=True,
    )
    held = IPv4Address(address=ipam, pool=pool, allocation_type="static")
    target = IPv4Address(address="10.93.0.5", pool=pool, allocation_type="static")
    other = (
        IPv4Address(address=served, pool=pool, allocation_type="static")
        if (served != ipam)
        else None
    )
    assignment = IPAssignment(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        ip_version=IPVersion.ipv4,
        ipv4_address=held,
        is_active=True,
        is_primary=True,
    )
    block = IpBlock(pool=pool, cidr="10.93.0.0/29", is_active=True)
    db_session.add_all(
        [pool, block, held, target, assignment] + ([other] if other else [])
    )
    db_session.commit()
    return block, assignment


def _patches(subscription, radius_ip):
    return (
        patch(
            "app.services.ip_consistency_audit._external_ip_state",
            return_value=({LOGIN: radius_ip}, {LOGIN}, 0),
        ),
        patch(
            "app.services.radius_projection_planner.plan_login_radius_projections",
            return_value={
                LOGIN: SimpleNamespace(
                    subscription_id=str(subscription.id),
                    plan=SimpleNamespace(mode="active", write_radreply=True),
                )
            },
        ),
        patch("app.services.ip_assignment_lifecycle.emit_event"),
    )


def _active_ips(db_session, subscription):
    return sorted(
        db_session.query(IPv4Address.address)
        .join(IPAssignment, IPAssignment.ipv4_address_id == IPv4Address.id)
        .filter(
            IPAssignment.subscription_id == subscription.id,
            IPAssignment.is_active.is_(True),
        )
        .all()
    )


def test_happy_path_moves_ipam_and_projection_together(db_session, subscription):
    block, _ = _setup(db_session, subscription)
    p1, p2, p3 = _patches(subscription, "10.93.0.4")
    with p1, p2, p3:
        result = web_service.replace_subscription_ipv4_with_owner(
            db_session,
            subscription_id=str(subscription.id),
            selector=str(block.id),
            requested_ip="10.93.0.5",
            actor_id="admin-test",
        )
    assert result == "10.93.0.5"
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "10.93.0.5"
    assert _active_ips(db_session, subscription) == [("10.93.0.5",)]


def test_projection_refusal_rolls_back_the_ipam_move(db_session, subscription):
    block, _ = _setup(db_session, subscription)
    p1, p2, p3 = _patches(subscription, "10.93.0.4")
    real = lifecycle.preview_service_ipv4_projection_repair
    calls = {"n": 0}

    def refuse_inside_command(*args, **kwargs):
        # Calls from the web preflight use the web module's own binding; only
        # the owner command's post-assignment preview goes through this one.
        calls["n"] += 1
        preview = real(*args, **kwargs)
        return replace(
            preview,
            decision=lifecycle.IPv4ServedProjectionDecision.session_observation_conflict,
        )

    with (
        p1,
        p2,
        p3,
        patch.object(
            lifecycle,
            "preview_service_ipv4_projection_repair",
            side_effect=refuse_inside_command,
        ),
        pytest.raises(lifecycle.IPv4AssignmentLifecycleError) as excinfo,
    ):
        web_service.replace_subscription_ipv4_with_owner(
            db_session,
            subscription_id=str(subscription.id),
            selector=str(block.id),
            requested_ip="10.93.0.5",
            actor_id="admin-test",
        )
    assert calls["n"] == 1
    assert "IPAM was not changed" in excinfo.value.message
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "10.93.0.4"
    assert _active_ips(db_session, subscription) == [("10.93.0.4",)]


def test_guard_refuses_when_customer_is_served_correctly_and_only_ipam_differs(
    db_session, subscription
):
    # Column and live session say .4; IPAM says .6 (wrong record).
    block, _ = _setup(db_session, subscription, served="10.93.0.4", ipam="10.93.0.6")
    db_session.add(
        RadiusActiveSession(
            subscription_id=subscription.id,
            subscriber_id=subscription.subscriber_id,
            username=LOGIN,
            acct_session_id="s1",
            framed_ip_address="10.93.0.4",
            session_start=datetime.now(UTC),
        )
    )
    db_session.commit()
    p1, p2, p3 = _patches(subscription, "10.93.0.4")
    with p1, p2, p3, pytest.raises(ValueError, match="repair the IPAM record instead"):
        web_service.replace_subscription_ipv4_with_owner(
            db_session,
            subscription_id=str(subscription.id),
            selector=str(block.id),
            requested_ip="10.93.0.5",
            actor_id="admin-test",
        )
    db_session.refresh(subscription)
    assert subscription.ipv4_address == "10.93.0.4"
    assert _active_ips(db_session, subscription) == [("10.93.0.6",)]
