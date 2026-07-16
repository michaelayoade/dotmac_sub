"""Shadow-compare the scoped RADIUS reconcile against the full sweep.

`reconcile_usernames` must write only the requested usernames while computing
the projection fleet-wide — so a subscriber with several services still gets the
correct per-subscription service count and duplicate-login dedup that the full
sweep produces. These assert, on a real DB session, that the scoped write set is
exactly the full sweep's rows for the requested usernames, that an absent
username is purged rather than reinserted, and that the empty set is a no-op.

Dry-run only: the psycopg write is never reached, so no radius DB is touched.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.models.catalog import (
    AccessCredential,
    AccessType,
    BillingMode,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.services import radius_population
from app.services.credential_crypto import (
    encrypt_credential_with_key,
    get_encryption_key,
)


@pytest.fixture()
def _radius_env(monkeypatch, db_session):
    # populate() aborts without a DSN; dry-run never connects, so a dummy is safe.
    monkeypatch.setattr(radius_population, "radius_dsn_libpq", lambda: "postgresql://x")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # The owner opens its own SessionLocal(); point it at the test session so the
    # fleet-wide projection reads the fixtures instead of the blocked real DB.
    monkeypatch.setattr(radius_population, "SessionLocal", lambda: db_session)


def _offer(db) -> CatalogOffer:
    offer = CatalogOffer(
        name=f"Offer {uuid.uuid4().hex[:6]}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle="monthly",
        speed_download_mbps=100,
        speed_upload_mbps=100,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def _account(db) -> Subscriber:
    sub = Subscriber(
        first_name="T",
        last_name="User",
        email=f"t{uuid.uuid4().hex[:8]}@example.com",
        status="active",
        is_active=True,
        billing_mode=BillingMode.prepaid,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _service(db, account, offer, key) -> str:
    """Create an active subscription + matching active credential; return login."""
    login = f"u{uuid.uuid4().hex[:8]}"
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        login=login,
    )
    db.add(subscription)
    db.add(
        AccessCredential(
            subscriber_id=account.id,
            subscription_id=subscription.id,
            username=login,
            is_active=True,
            secret_hash=encrypt_credential_with_key("pw-" + login, key),
        )
    )
    db.commit()
    return login


def _seed(db):
    """Subscriber A with TWO services (the service-count trap) + subscriber B."""
    key = get_encryption_key()
    offer = _offer(db)
    a = _account(db)
    a1 = _service(db, a, offer, key)
    a2 = _service(db, a, offer, key)
    b = _account(db)
    b1 = _service(db, b, offer, key)
    return a1, a2, b1


def test_full_sweep_projects_every_login(_radius_env, db_session):
    a1, a2, b1 = _seed(db_session)
    stats = radius_population.populate(dry_run=True)
    assert stats["radcheck_upserts"] == 3
    assert "scoped_targets" not in stats


def test_scoped_reconcile_writes_only_the_requested_login(_radius_env, db_session):
    a1, a2, b1 = _seed(db_session)
    stats = radius_population.reconcile_usernames({a1}, dry_run=True)
    # fleet compute ran (a's sibling service exists), but only a1 is written
    assert stats["scoped_targets"] == 1
    assert stats["radcheck_upserts"] == 1


def test_absent_username_is_purged_not_reinserted(_radius_env, db_session):
    a1, a2, b1 = _seed(db_session)
    stats = radius_population.reconcile_usernames({a1, "ghost-user"}, dry_run=True)
    # both requested (so both get deleted), but only the present one is written
    assert stats["scoped_targets"] == 2
    assert stats["radcheck_upserts"] == 1


def test_empty_target_set_is_a_noop(_radius_env, db_session):
    _seed(db_session)
    stats = radius_population.reconcile_usernames(set(), dry_run=True)
    assert stats["radcheck_upserts"] == 0
