"""Tests for bandwidth service."""

from datetime import datetime, timezone, timedelta

from app.schemas.bandwidth import BandwidthSampleCreate, BandwidthSampleUpdate
from app.services import bandwidth as bandwidth_service


def test_create_bandwidth_sample(db_session, subscription):
    """Test creating a bandwidth sample."""
    sample = bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=1000000,
            tx_bps=500000,
            sample_at=datetime.now(timezone.utc),
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
            sample_at=datetime.now(timezone.utc),
        ),
    )
    bandwidth_service.bandwidth_samples.create(
        db_session,
        BandwidthSampleCreate(
            subscription_id=subscription.id,
            rx_bps=200000,
            tx_bps=100000,
            sample_at=datetime.now(timezone.utc),
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
            sample_at=datetime.now(timezone.utc),
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
            sample_at=datetime.now(timezone.utc),
        ),
    )
    bandwidth_service.bandwidth_samples.delete(db_session, str(sample.id))
    # Bandwidth samples use hard delete, not soft delete
    from fastapi import HTTPException
    import pytest
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
            sample_at=datetime.now(timezone.utc),
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
            sample_at=datetime.now(timezone.utc),
        ),
    )
    assert sample.device_id == network_device.id


def test_list_bandwidth_samples_order_by_sample_at(db_session, subscription):
    """Test listing bandwidth samples ordered by sample_at."""
    now = datetime.now(timezone.utc)
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
