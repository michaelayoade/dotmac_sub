"""Expiry keeps status writers serialized without blocking bandwidth FK reads."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.bandwidth import BandwidthSample
from app.models.catalog import (
    AccessRequirement,
    AccessType,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.catalog import (
    CatalogOfferCreate,
    OfferVersionCreate,
    SubscriptionCreate,
)
from app.services import catalog as catalog_service
from app.services.account_lifecycle import expire_subscription
from app.services.catalog.offer_access_requirement import SystemAdmission
from app.services.subscriber import _default_reseller_id


def _seed_subscription(session_factory: Callable[[], Session]) -> UUID:
    suffix = uuid4().hex[:12]
    with session_factory() as setup:
        subscriber = Subscriber(
            first_name="Expiry",
            last_name="LockCompatibility",
            email=f"expiry-lock-{suffix}@example.com",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.commit()
        offer = catalog_service.offers.create(
            setup,
            CatalogOfferCreate(
                name=f"Expiry Lock Compatibility {suffix}",
                code=f"EXPIRY-LOCK-{suffix}",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
        )
        offer_id = offer.id
        setup.commit()
        catalog_service.offer_versions.create(
            setup,
            OfferVersionCreate(
                access_requirement=AccessRequirement.unclassified,
                offer_id=offer_id,
                version_number=1,
                name=f"Expiry Lock Compatibility {suffix} v1",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            ),
            principal=SystemAdmission(reason="test fixture"),
        )
        subscription = catalog_service.subscriptions.create(
            setup,
            SubscriptionCreate(
                account_id=subscriber.id,
                offer_id=offer_id,
                status=SubscriptionStatus.active,
                start_at=datetime(2026, 8, 1, tzinfo=UTC),
                next_billing_at=datetime(2026, 9, 1, tzinfo=UTC),
            ),
        )
        subscription_id = subscription.id
        # Warm the same shape with FOR UPDATE before expiry requests NO KEY UPDATE.
        # The pinned SQLAlchemy cache key does not distinguish key_share.
        setup.execute(
            select(Subscription)
            .where(Subscription.id == str(subscription_id))
            .with_for_update()
        ).scalar_one()
        setup.commit()
        return subscription_id


def test_expiry_allows_bandwidth_insert_and_blocks_competing_status_writer(
    engine: Engine,
) -> None:
    session_factory: Callable[[], Session] = partial(
        SessionLocal, bind=engine, autoflush=False, expire_on_commit=False
    )
    subscription_id = _seed_subscription(session_factory)
    expiry_locked = Event()
    checked = Event()

    def expire_uncommitted() -> None:
        with session_factory() as owner:
            expire_subscription(owner, str(subscription_id), emit=False)
            subscription = owner.get(Subscription, subscription_id)
            assert subscription is not None
            assert subscription.status is SubscriptionStatus.expired
            expiry_locked.set()
            try:
                assert checked.wait(timeout=10), (
                    "concurrent expiry checks did not finish"
                )
            finally:
                owner.rollback()

    def verify_concurrency() -> None:
        assert expiry_locked.wait(timeout=10), "expiry did not acquire its row lock"
        try:
            with session_factory() as observations:
                observations.execute(text("SET LOCAL lock_timeout = '2s'"))
                observations.add(
                    BandwidthSample(
                        subscription_id=subscription_id,
                        rx_bps=1,
                        tx_bps=1,
                        sample_at=datetime(2026, 8, 1, tzinfo=UTC),
                    )
                )
                observations.commit()

            with session_factory() as competing_writer:
                competing_writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
                with pytest.raises(OperationalError) as captured:
                    competing_writer.scalar(
                        select(Subscription)
                        .where(Subscription.id == subscription_id)
                        .with_for_update(key_share=True),
                        execution_options={"compiled_cache": None},
                    )
                assert getattr(captured.value.orig, "sqlstate", None) == "55P03"
                competing_writer.rollback()
        finally:
            checked.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        expiry = pool.submit(expire_uncommitted)
        verification = pool.submit(verify_concurrency)
        expiry.result(timeout=20)
        verification.result(timeout=20)
