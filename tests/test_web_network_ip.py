from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models.network import IPAssignment, IpBlock, IpPool, IPv4Address, IPVersion
from app.services import web_network_ip


class FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def options(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping
        self.commits = 0

    def query(self, model):
        return FakeQuery(self.mapping.get(model, []))

    def commit(self):
        self.commits += 1


def test_pool_utilization_counts_only_active_assignments(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Range A",
        ip_version=IPVersion.ipv4,
        cidr="10.0.0.0/30",
        is_active=True,
    )
    assigned = IPv4Address(
        id=uuid.uuid4(),
        address="10.0.0.1",
        pool_id=pool.id,
        is_reserved=False,
    )
    reserved = IPv4Address(
        id=uuid.uuid4(),
        address="10.0.0.2",
        pool_id=pool.id,
        is_reserved=True,
    )
    assigned.assignment = IPAssignment(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        ip_version=IPVersion.ipv4,
        ipv4_address_id=assigned.id,
        is_active=True,
    )
    reserved.assignment = None

    db = FakeSession({IPv4Address: [assigned, reserved]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_pools,
        "list",
        lambda **_kwargs: [pool],
    )
    monkeypatch.setattr(
        web_network_ip.network_service.ip_blocks,
        "list",
        lambda **_kwargs: [],
    )

    state = web_network_ip.build_ip_pools_data(db)
    util = state["pool_utilization"][str(pool.id)]

    assert util["total"] == 2
    assert util["used"] == 1
    assert util["reserved"] == 1
    assert util["available"] == 0
    assert util["percent"] == 50


def test_reconcile_ipv4_pool_memberships_maps_existing_addresses_to_pool(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Legacy Range",
        ip_version=IPVersion.ipv4,
        cidr="172.16.100.0/24",
        is_active=True,
    )
    address = IPv4Address(
        id=uuid.uuid4(),
        address="172.16.100.25",
        pool_id=None,
        is_reserved=False,
    )
    db = FakeSession({IPv4Address: [address]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_pools,
        "list",
        lambda **_kwargs: [pool],
    )

    result = web_network_ip.reconcile_ipv4_pool_memberships(db)

    assert result["updated"] == 1
    assert address.pool_id == pool.id
    assert db.commits == 1


def test_ipv4_block_detail_marks_assigned_reserved_and_available(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Range B",
        ip_version=IPVersion.ipv4,
        cidr="10.20.30.0/29",
        is_active=True,
        notes="router=core-r1",
    )
    block = IpBlock(
        id=uuid.uuid4(),
        pool_id=pool.id,
        cidr="10.20.30.0/30",
        is_active=True,
    )
    block.pool = pool

    subscriber = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Test Subscriber",
        first_name="Test",
        last_name="Subscriber",
        email="subscriber@example.com",
    )
    subscription = SimpleNamespace(id=uuid.uuid4(), service_id="SVC-100")

    assigned = IPv4Address(
        id=uuid.uuid4(),
        address="10.20.30.1",
        pool_id=pool.id,
        is_reserved=False,
    )
    reserved = IPv4Address(
        id=uuid.uuid4(),
        address="10.20.30.2",
        pool_id=pool.id,
        is_reserved=True,
    )
    assigned.assignment = IPAssignment(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        ip_version=IPVersion.ipv4,
        ipv4_address_id=assigned.id,
        is_active=True,
    )
    assigned.assignment.__dict__["subscriber"] = subscriber
    assigned.assignment.__dict__["subscription"] = subscription
    reserved.assignment = None

    db = FakeSession({IPv4Address: [assigned, reserved]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_blocks,
        "get",
        lambda **_kwargs: block,
    )

    state = web_network_ip.build_ipv4_block_detail_data(db, block_id=str(block.id), limit=10)

    assert state is not None
    rows = {row["ip_address"]: row["status"] for row in state["ip_rows"]}
    assert rows["10.20.30.1"] == "assigned"
    assert rows["10.20.30.2"] == "reserved"
    assert state["stats"]["assigned"] == 1
    assert state["stats"]["reserved"] == 1
    assert state["stats"]["available"] == 0
