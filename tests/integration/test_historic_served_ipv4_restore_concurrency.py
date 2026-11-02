"""Restore locking evidence on an isolated migration-created PostgreSQL clone."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.network import IPAssignment, IpPool, IPv4Address, IPVersion
from app.models.subscriber import Subscriber
from app.services import ip_assignment_lifecycle as lifecycle
from app.services.owner_commands import CommandContext
from app.services.provisioning_helpers import _find_available_address
from app.services.subscriber import _default_reseller_id

pytestmark = pytest.mark.integration
HISTORIC = "198.51.100.20"


@pytest.fixture
def restore_world(cloned_database):
    engine = create_engine(cloned_database("heads"))
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with factory() as db:
            suffix = uuid4().hex
            reseller_id = _default_reseller_id(db)
            account = Subscriber(
                first_name="Restore",
                reseller_id=reseller_id,
                last_name="Race",
                email=f"{suffix}@example.com",
            )
            other = Subscriber(
                first_name="Other",
                reseller_id=reseller_id,
                last_name="Race",
                email=f"other-{suffix}@example.com",
            )
            offer = CatalogOffer(
                name=f"Restore {suffix}",
                service_type=ServiceType.residential,
                access_type=AccessType.fiber,
                price_basis=PriceBasis.flat,
            )
            pool = IpPool(
                name=f"Restore {suffix}",
                ip_version=IPVersion.ipv4,
                cidr="198.51.100.0/24",
                is_active=True,
            )
            db.add_all([account, other, offer, pool])
            db.flush()
            service = Subscription(
                subscriber_id=account.id,
                offer_id=offer.id,
                status=SubscriptionStatus.disabled,
                ipv4_address=HISTORIC,
            )
            historic = IPv4Address(address=HISTORIC, pool_id=pool.id, is_reserved=False)
            own = IPv4Address(
                address="198.51.100.40", pool_id=pool.id, is_reserved=False
            )
            db.add_all([service, historic, own])
            db.flush()
            db.add(
                IPAssignment(
                    subscriber_id=account.id,
                    subscription_id=service.id,
                    ip_version=IPVersion.ipv4,
                    ipv4_address_id=own.id,
                    is_active=True,
                    is_primary=True,
                )
            )
            db.commit()
            preview = lifecycle.preview_historic_served_ipv4_clear(
                db, subscription_id=service.id
            )
            assert preview.applicable
            db.commit()
            lifecycle.clear_historic_served_ipv4(
                db,
                lifecycle.ClearHistoricServedIPv4Command(
                    context=_context("clear"),
                    subscription_id=service.id,
                    preview_fingerprint=preview.fingerprint,
                ),
            )
            reviewed = lifecycle.preview_cleared_served_ipv4_restore(
                db, subscription_id=service.id
            )
            assert reviewed.applicable
            db.commit()
            yield factory, service.id, other.id, historic.id, pool.id, reviewed
    finally:
        engine.dispose()


def _context(key):
    return CommandContext.system(
        actor="pytest:restore-race",
        scope="ipam_historic_served_ipv4",
        reason="Verify restore serialization",
        idempotency_key=key,
    )


def _command(service_id, reviewed):
    return lifecycle.RestoreClearedServedIPv4Command(
        context=_context("restore"),
        subscription_id=service_id,
        preview_fingerprint=reviewed.fingerprint,
    )


def test_allocation_between_restore_read_and_lock_is_refused(
    restore_world, monkeypatch
):
    factory, service_id, other_id, historic_id, pool_id, reviewed = restore_world
    original = lifecycle.preview_cleared_served_ipv4_restore
    reads = 0

    def allocate_after_first_read(db, *, subscription_id):
        nonlocal reads
        result = original(db, subscription_id=subscription_id)
        reads += 1
        if reads == 1:
            # Restore has read ready_restore but has not locked the free row.
            with factory() as allocator:
                picked = _find_available_address(allocator, IPVersion.ipv4, pool_id)
                assert picked.id == historic_id
                allocator.add(
                    IPAssignment(
                        subscriber_id=other_id,
                        ip_version=IPVersion.ipv4,
                        ipv4_address_id=picked.id,
                        is_active=True,
                    )
                )
                allocator.commit()
        return result

    monkeypatch.setattr(
        lifecycle, "preview_cleared_served_ipv4_restore", allocate_after_first_read
    )
    with factory() as db:
        with pytest.raises(lifecycle.IPAssignmentOwnershipError) as exc:
            lifecycle.restore_cleared_served_ipv4(db, _command(service_id, reviewed))
        assert exc.value.code.endswith("stale_preview")
        db.rollback()
        assert db.get(Subscription, service_id).ipv4_address is None
        held = db.scalar(
            select(IPAssignment).where(IPAssignment.ipv4_address_id == historic_id)
        )
        assert held.subscriber_id == other_id
    assert reads == 2


def test_restore_locks_exclude_allocator_and_inventory_writer(
    restore_world, monkeypatch
):
    factory, service_id, _, historic_id, pool_id, reviewed = restore_world
    original = lifecycle._write_served_ipv4_preserving_updated_at
    checked = False

    def verify_competitors_before_write(db, subscription, value):
        nonlocal checked
        # Independent transactions overlap the restore's final check/write.
        with factory() as allocator:
            assert _find_available_address(allocator, IPVersion.ipv4, pool_id) is None
            allocator.rollback()
        with factory() as inventory_writer:
            with pytest.raises(OperationalError) as exc:
                inventory_writer.execute(
                    text("LOCK TABLE network_devices IN ROW EXCLUSIVE MODE NOWAIT")
                )
            assert getattr(exc.value.orig, "sqlstate", None) == "55P03"
            inventory_writer.rollback()
        checked = True
        original(db, subscription, value)

    monkeypatch.setattr(
        lifecycle,
        "_write_served_ipv4_preserving_updated_at",
        verify_competitors_before_write,
    )
    with factory() as db:
        outcome = lifecycle.restore_cleared_served_ipv4(
            db, _command(service_id, reviewed)
        )
        assert checked and outcome.restored_ipv4 == HISTORIC
        assert db.get(Subscription, service_id).ipv4_address == HISTORIC
        assert (
            db.scalar(
                select(IPAssignment.id).where(
                    IPAssignment.ipv4_address_id == historic_id
                )
            )
            is None
        )
