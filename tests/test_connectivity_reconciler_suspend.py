"""Tests for the suspend dimension + idempotent-sync signature (shadow slice).

Covers the design-doc invariants (CONNECTIVITY_STATE_MACHINE.md §2 INV-1..5),
the desired-state signature used as the RADIUS-sync idempotency key, and the
Redis-backed enqueue gate. Everything here is shadow: nothing mutates live
connectivity.
"""

from __future__ import annotations

from app.models.catalog import (
    AccessCredential,
    AccessState,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services.connectivity_reconciler import (
    _should_enqueue_refresh,
    converge_subscription_connectivity,
    derive_desired_connectivity,
    desired_connectivity_signature,
    plan_subscription_suspend,
)


def _seed_sub(db_session, *, login, offer, status=SubscriptionStatus.active,
              col_ip=None, with_active_credential=False):
    subscriber = Subscriber(
        first_name="Susp", last_name="Case", email=f"{login}@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=status,
        login=login,
        ipv4_address=col_ip,
    )
    db_session.add(sub)
    db_session.flush()
    if with_active_credential:
        db_session.add(
            AccessCredential(
                subscriber_id=subscriber.id, username=login, is_active=True
            )
        )
    db_session.commit()
    return sub


# ---------------------------------------------------------------------------
# Incident-shaped invariants (pure derivation — design §2 / §5.3)
# ---------------------------------------------------------------------------


def test_paid_customer_keeps_ip_across_suspend():
    """INV-1 (#282): the IP is retained in active AND suspended; only terminal
    releases it."""
    active = derive_desired_connectivity(SubscriptionStatus.active)
    suspended = derive_desired_connectivity(SubscriptionStatus.suspended)
    assert active.ip_retained is True
    assert suspended.ip_retained is True  # paid→offline keeps the address


def test_suspended_keeps_credential_and_kicks_once():
    """INV-3 (credential survives suspend, reversible) + INV-5 (CoA once)."""
    suspended = derive_desired_connectivity(SubscriptionStatus.suspended)
    assert suspended.credentials_active is True
    assert suspended.access_state is AccessState.captive
    assert suspended.kick_live_session is True


def test_terminal_releases_everything():
    """INV-3/INV-4: cancel deactivates creds, releases IP, drops the cache."""
    canceled = derive_desired_connectivity(SubscriptionStatus.canceled)
    assert canceled.credentials_active is False
    assert canceled.ip_active is False
    assert canceled.ip_retained is False
    assert canceled.access_state is AccessState.terminated


def test_fraud_hard_reject_is_suspended_not_captive():
    """A fraud lock projects ``suspended`` (Auth-Type Reject), not captive."""
    normal = derive_desired_connectivity(SubscriptionStatus.suspended)
    fraud = derive_desired_connectivity(
        SubscriptionStatus.suspended, hard_reject=True
    )
    assert normal.access_state is AccessState.captive
    assert fraud.access_state is AccessState.suspended


# ---------------------------------------------------------------------------
# Suspend dimension — shadow plan via the converger
# ---------------------------------------------------------------------------


def test_converge_suspended_returns_suspend_shadow_plan(db_session, catalog_offer):
    sub = _seed_sub(
        db_session, login="s1", offer=catalog_offer,
        status=SubscriptionStatus.suspended,
    )
    plan = converge_subscription_connectivity(db_session, str(sub.id), apply=False)
    assert plan["dimension"] == "suspend"
    assert plan["applied"] is False
    assert plan["desired_access_state"] == "captive"
    kinds = {a["kind"] for a in plan["actions"]}
    # access_state None on the row → would set captive; no IP → retention violated.
    assert "set_access_state" in kinds
    assert "ip_retention_violated" in kinds
    assert "kick_live_session" in kinds


def test_converge_suspended_with_credential_no_ensure_action(
    db_session, catalog_offer
):
    sub = _seed_sub(
        db_session, login="s2", offer=catalog_offer,
        status=SubscriptionStatus.suspended, col_ip="10.0.0.5",
        with_active_credential=True,
    )
    plan = plan_subscription_suspend(db_session, sub)
    kinds = {a["kind"] for a in plan["actions"]}
    # has an active credential AND a retained IP → neither violation fires.
    assert "ensure_credentials_active" not in kinds
    assert "ip_retention_violated" not in kinds


def test_converge_suspended_writes_nothing(db_session, catalog_offer):
    sub = _seed_sub(
        db_session, login="s3", offer=catalog_offer,
        status=SubscriptionStatus.suspended,
    )
    before = sub.access_state
    converge_subscription_connectivity(db_session, str(sub.id), apply=True)
    db_session.refresh(sub)
    assert sub.access_state == before  # apply is a no-op for suspend in this slice


# ---------------------------------------------------------------------------
# Desired-state signature (idempotency key)
# ---------------------------------------------------------------------------


def test_signature_is_deterministic(db_session, catalog_offer):
    sub = _seed_sub(db_session, login="sig1", offer=catalog_offer)
    sid = sub.subscriber_id
    a = desired_connectivity_signature(db_session, sid)
    b = desired_connectivity_signature(db_session, sid)
    assert a == b and len(a) == 16


def test_signature_changes_with_status(db_session, catalog_offer):
    sub = _seed_sub(db_session, login="sig2", offer=catalog_offer)
    sid = sub.subscriber_id
    before = desired_connectivity_signature(db_session, sid)
    sub.status = SubscriptionStatus.suspended
    db_session.commit()
    after = desired_connectivity_signature(db_session, sid)
    assert before != after


# ---------------------------------------------------------------------------
# Idempotent enqueue gate
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value


def test_enqueue_gate_skips_unchanged_signature(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(
        "app.services.connectivity_reconciler._shadow_redis", lambda: fake
    )
    sid = "sub-xyz"
    assert _should_enqueue_refresh(sid, "abc123") is True   # first time → enqueue
    assert _should_enqueue_refresh(sid, "abc123") is False  # unchanged → skip
    assert _should_enqueue_refresh(sid, "def456") is True   # changed → enqueue


def test_enqueue_gate_allows_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(
        "app.services.connectivity_reconciler._shadow_redis", lambda: None
    )
    # No idempotency store → never suppress a real refresh.
    assert _should_enqueue_refresh("s", "abc") is True
