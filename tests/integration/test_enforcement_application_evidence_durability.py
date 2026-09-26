"""Enforcement evidence must outlive the transaction that performed the attempt.

ADR 0017: a router change is irreversible, so the ``EnforcementApplication``
record of it is written by ``access.session_enforcement`` on a genuinely
independent connection (``db_session_adapter.create_session()``). The event
dispatcher's isolated handler session, the scheduled cleanup task and the FUP
lift owner command all roll back on failure; a same-session write would be
erased in exactly the failure case the record exists to capture.

Only a REAL, separate PostgreSQL connection can prove this. SQLite's
``StaticPool`` unit lane shares one physical connection, so a "second" session
there sits inside the same uncommitted transaction. Requires
``TEST_DATABASE_URL`` (``make test-db-up && make test-integration``); the
integration conftest points ``db_session_adapter`` at the same database.

The dispatcher, Celery and FUP callers all reach the evidence through the same
per-NAS helper and writer exercised here, so rolling back the caller's own
transaction around the real helper proves the property each of them depends on.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import patch

from routeros_api.exceptions import RouterOsApiCommunicationError
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    NasDevice,
    NasVendor,
    OfferStatus,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.enforcement_application import (
    EnforcementApplication,
    EnforcementEffect,
    EnforcementFailureClass,
    EnforcementOutcomeValue,
    EnforcementPath,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.enforcement import (
    EnforcementOutcome,
    _enforce_address_list_on_nas,
    _record_enforcement_application,
)

_SECRET = "IntegrationSecret123"


def _login_rejection() -> RouterOsApiCommunicationError:
    """The exact shape routeros_api raised for Eagle FM Access (2026-09-17)."""
    return RouterOsApiCommunicationError(
        'Error "invalid user name or password (6)" executing command '
        f"b'/login =name=Eagle_API =password={_SECRET} .tag=1'",
        b"invalid user name or password (6)",
    )


def _api_only_mikrotik() -> NasDevice:
    """A transient API-only MikroTik NAS: no SSH credentials, as most are."""
    return NasDevice(
        id=uuid.uuid4(),
        name="Integration Eagle",
        vendor=NasVendor.mikrotik,
        ip_address="192.0.2.10",
        management_ip="192.0.2.10",
        ssh_username=None,
    )


def test_evidence_survives_the_callers_rollback(engine):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    subscription_id = uuid.uuid4()
    nas = _api_only_mikrotik()

    # Sensitivity: the fixture really carries the secret the record must drop.
    assert _SECRET in str(_login_rejection())

    with session_factory() as caller:
        caller.begin()
        with (
            patch("app.services.enforcement._nas_with_api_creds", return_value=nas),
            patch(
                "app.services.nas._mikrotik.apply_mikrotik_address_list_via_api",
                side_effect=_login_rejection(),
            ),
        ):
            result = _enforce_address_list_on_nas(
                caller,
                nas,
                "suspended",
                "10.20.30.40",
                add=True,
                subscription_id=subscription_id,
            )
        assert result is False
        # The caller's unit of work fails and rolls back, as the dispatcher's
        # isolated handler session, the cleanup task and the FUP owner do.
        caller.rollback()

    with session_factory() as observer:
        row = observer.execute(
            select(EnforcementApplication).where(
                EnforcementApplication.subscription_id == subscription_id,
                EnforcementApplication.nas_device_id == nas.id,
                EnforcementApplication.effect
                == EnforcementEffect.address_list_block.value,
            )
        ).scalar_one_or_none()
        assert row is not None, "the evidence was erased by the caller's rollback"
        assert row.outcome == EnforcementOutcomeValue.failed.value
        assert row.failure_class == EnforcementFailureClass.auth_rejected.value
        assert row.attempt_count == 1
        assert row.first_failed_at is not None
        assert _SECRET not in (row.detail or "")


def test_evidence_write_does_not_wait_on_the_callers_subscription_lock(engine):
    """Callers hold ``SELECT ... FOR UPDATE`` on the subscription (e.g.
    ``fup_state.py``, ``account_lifecycle.py``). The out-of-band write must not
    block on that lock; the table deliberately has no foreign keys (ADR 0017
    section 3), and this pins that no future FK or trigger reintroduces a wait.
    """

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Enforcement Evidence Lock {suffix}",
            code=f"enf-evidence-lock-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Enforcement",
            last_name="EvidenceLock",
            email=f"enf-evidence-lock-{suffix}@example.com",
            reseller=reseller,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Enforcement Evidence Lock Plan {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            status=OfferStatus.active,
            is_active=True,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
        )
        setup.add(subscription)
        setup.commit()
        subscription_id = subscription.id

    nas_device_id = uuid.uuid4()
    with session_factory() as holder:
        holder.execute(
            text("SELECT id FROM subscriptions WHERE id = :id FOR UPDATE"),
            {"id": subscription_id},
        )

        started = time.monotonic()
        _record_enforcement_application(
            subscription_id=subscription_id,
            nas_device_id=nas_device_id,
            effect=EnforcementEffect.address_list_block,
            outcome=EnforcementOutcome.failed_from(
                _login_rejection(), path=EnforcementPath.api
            ),
        )
        elapsed = time.monotonic() - started
        # The writer's own lock_timeout is 2s and it never raises, so a wait
        # would show up as a slow return with no row, not as an exception.
        assert elapsed < 1.5, f"evidence write waited {elapsed:.2f}s on the lock"

        holder.rollback()

    with session_factory() as observer:
        row = observer.execute(
            select(EnforcementApplication).where(
                EnforcementApplication.subscription_id == subscription_id,
                EnforcementApplication.nas_device_id == nas_device_id,
            )
        ).scalar_one_or_none()
        assert row is not None, "the evidence write did not complete under the lock"
        assert row.failure_class == EnforcementFailureClass.auth_rejected.value
