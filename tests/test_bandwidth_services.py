"""Tests for bandwidth service."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.catalog import NasDevice
from app.schemas.bandwidth import BandwidthSampleCreate, BandwidthSampleUpdate
from app.services import bandwidth as bandwidth_service
from app.tasks import bandwidth as bandwidth_tasks


def test_create_bandwidth_sample(db_session, subscription):
    """Test creating a bandwidth sample."""
    sample = bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=1000000,
            tx_bps=500000,
            sample_at=datetime.now(UTC),
        ),
    )
    assert sample.subscription_id == subscription.id
    assert sample.rx_bps == 1000000
    assert sample.tx_bps == 500000


def test_list_bandwidth_samples_by_subscription(db_session, subscription):
    """Test listing bandwidth samples by subscription."""
    bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=100000,
            tx_bps=50000,
            sample_at=datetime.now(UTC),
        ),
    )
    bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=200000,
            tx_bps=100000,
            sample_at=datetime.now(UTC),
        ),
    )

    samples = bandwidth_service.bandwidth_samples.list(
        db_session,
        subscription_id=str(subscription.id),
        device_id=None,
        interface_id=None,
        order_by="created_at",
        order_dir="asc",
        limit=10,
        offset=0,
    )
    assert len(samples) >= 2
    assert all(str(s.subscription_id) == str(subscription.id) for s in samples)


def test_update_bandwidth_sample(db_session, subscription):
    """Test updating a bandwidth sample."""
    sample = bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=100000,
            tx_bps=50000,
            sample_at=datetime.now(UTC),
        ),
    )
    updated = bandwidth_service.bandwidth_samples.update(
        db_session,
        str(sample.id),
        BandwidthSampleUpdate(rx_bps=150000, tx_bps=75000),
    )
    assert updated.rx_bps == 150000
    assert updated.tx_bps == 75000


def test_delete_bandwidth_sample(db_session, subscription):
    """Test deleting a bandwidth sample."""
    sample = bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=100000,
            tx_bps=50000,
            sample_at=datetime.now(UTC),
        ),
    )
    bandwidth_service.bandwidth_samples.delete(db_session, str(sample.id))
    # Bandwidth samples use hard delete, not soft delete
    with pytest.raises(HTTPException) as exc_info:
        bandwidth_service.bandwidth_samples.get(db_session, str(sample.id))
    assert exc_info.value.status_code == 404


def test_get_bandwidth_sample(db_session, subscription):
    """Test getting a bandwidth sample by ID."""
    sample = bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=500000,
            tx_bps=250000,
            sample_at=datetime.now(UTC),
        ),
    )
    fetched = bandwidth_service.bandwidth_samples.get(db_session, str(sample.id))
    assert fetched is not None
    assert fetched.id == sample.id
    assert fetched.rx_bps == 500000


def test_bandwidth_sample_with_device(db_session, subscription, network_device):
    """Test creating a bandwidth sample linked to a device."""
    sample = bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            device_id=network_device.id,
            rx_bps=2000000,
            tx_bps=1000000,
            sample_at=datetime.now(UTC),
        ),
    )
    assert sample.device_id == network_device.id


def test_list_bandwidth_samples_order_by_sample_at(db_session, subscription):
    """Test listing bandwidth samples ordered by sample_at."""
    now = datetime.now(UTC)
    bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=100000,
            tx_bps=50000,
            sample_at=now - timedelta(hours=2),
        ),
    )
    bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=200000,
            tx_bps=100000,
            sample_at=now - timedelta(hours=1),
        ),
    )
    bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=300000,
            tx_bps=150000,
            sample_at=now,
        ),
    )

    samples = bandwidth_service.bandwidth_samples.list(
        db_session,
        subscription_id=str(subscription.id),
        device_id=None,
        interface_id=None,
        order_by="sample_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    assert len(samples) >= 3
    # Verify descending order (newest first)
    for i in range(len(samples) - 1):
        assert samples[i].sample_at >= samples[i + 1].sample_at


def test_check_subscription_access_allows_admin_role_in_roles(db_session, subscription):
    user = {"roles": ["admin"], "principal_type": "subscriber"}

    allowed = bandwidth_service.bandwidth_samples.check_subscription_access(
        db_session,
        subscription.id,
        user,
    )

    assert allowed.id == subscription.id


def test_check_subscription_access_allows_owner_principal_id(db_session, subscription):
    user = {
        "roles": [],
        "principal_type": "subscriber",
        "principal_id": str(subscription.subscriber_id),
    }

    allowed = bandwidth_service.bandwidth_samples.check_subscription_access(
        db_session,
        subscription.id,
        user,
    )

    assert allowed.id == subscription.id


def test_check_subscription_access_denies_non_owner_subscriber(
    db_session, subscription
):
    user = {
        "roles": [],
        "principal_type": "subscriber",
        "principal_id": str(uuid4()),
    }

    with pytest.raises(HTTPException) as exc_info:
        bandwidth_service.bandwidth_samples.check_subscription_access(
            db_session,
            subscription.id,
            user,
        )

    assert exc_info.value.status_code == 403


def test_get_user_active_subscription_uses_principal_id(db_session, subscription):
    user = {"principal_id": str(subscription.subscriber_id)}

    current = bandwidth_service.bandwidth_samples.get_user_active_subscription(
        db_session,
        user,
    )

    assert current.id == subscription.id


def test_get_user_active_subscription_allows_blocked_subscription(
    db_session, subscription
):
    from app.models.catalog import SubscriptionStatus

    subscription.status = SubscriptionStatus.blocked
    db_session.commit()

    current = bandwidth_service.bandwidth_samples.get_user_active_subscription(
        db_session,
        {"account_id": str(subscription.subscriber_id)},
    )

    assert current.id == subscription.id


def test_process_bandwidth_stream_resolves_network_device_from_nas(
    db_session, subscription, network_device
):
    subscription_id = subscription.id
    network_device_id = network_device.id
    nas = NasDevice(
        name="NAS-1",
        network_device_id=network_device_id,
    )
    db_session.add(nas)
    db_session.commit()

    class _FakeRedis:
        def xgroup_create(self, *_args, **_kwargs):
            return None

        def xreadgroup(self, *, streams, **_kwargs):
            if list(streams.values())[0] == "0":
                return []
            return [
                (
                    "bandwidth:samples",
                    [
                        (
                            b"1-0",
                            {
                                b"subscription_id": str(subscription_id).encode(),
                                b"nas_device_id": str(nas.id).encode(),
                                b"rx_bps": b"1000",
                                b"tx_bps": b"2000",
                                b"sample_at": datetime.now(UTC).isoformat().encode(),
                            },
                        )
                    ],
                )
            ]

        def xack(self, *_args, **_kwargs):
            return 1

        def close(self):
            return None

    from unittest.mock import patch

    with (
        patch("app.tasks.bandwidth._get_redis_client", return_value=_FakeRedis()),
        patch("app.tasks.bandwidth.SessionLocal", return_value=db_session),
    ):
        result = bandwidth_tasks.process_bandwidth_stream()

    assert result["processed"] == 1
    sample = bandwidth_service.bandwidth_samples.list(
        db_session,
        subscription_id=str(subscription_id),
        device_id=None,
        interface_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )[0]
    assert sample.device_id == network_device_id
