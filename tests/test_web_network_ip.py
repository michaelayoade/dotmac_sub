from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from app.models.network import (
    IPAssignment,
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    SubscriberAdditionalRoute,
)
from app.models.network_monitoring import NetworkDevice
from app.services import web_network_ip
from app.web.admin import network_ip_management


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


def _request(path: str = "/admin/network/ip-management") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_ip_management_search_keeps_addresses_tab(monkeypatch):
    monkeypatch.setattr(
        network_ip_management.web_network_ip_service,
        "build_ip_management_data",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        network_ip_management.web_network_ip_actions_service,
        "activity_for_types",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        network_ip_management,
        "_base_context",
        lambda request, db, active_page, active_menu="network": {"request": request},
    )
    monkeypatch.setattr(
        network_ip_management.templates,
        "TemplateResponse",
        lambda template, context: {"template": template, "context": context},
    )

    response = network_ip_management.ip_management(
        _request(),
        db=object(),
        search="102.220.189.16",
    )

    assert response["template"] == "admin/network/ip-management/index.html"
    assert response["context"]["tab"] == "addresses"


def test_ip_management_address_controls_preserve_addresses_tab():
    template = Path("templates/admin/network/ip-management/index.html").read_text()

    assert 'name="tab" value="addresses"' in template
    assert 'href="/admin/network/ip-management?tab=addresses"' in template


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


def test_pool_utilization_counts_ont_management_allocations_as_used(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Mgmt Range",
        ip_version=IPVersion.ipv4,
        cidr="10.0.1.0/30",
        is_active=True,
    )
    managed = IPv4Address(
        id=uuid.uuid4(),
        address="10.0.1.1",
        pool_id=pool.id,
        is_reserved=True,
        ont_unit_id=uuid.uuid4(),
        allocation_type="management",
    )
    reserved = IPv4Address(
        id=uuid.uuid4(),
        address="10.0.1.2",
        pool_id=pool.id,
        is_reserved=True,
    )
    managed.assignment = None
    reserved.assignment = None

    db = FakeSession({IPv4Address: [managed, reserved]})
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

    assert util["used"] == 1
    assert util["assigned"] == 1
    assert util["reserved"] == 1
    assert util["available"] == 0


def test_pool_and_block_utilization_count_network_device_ips(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Device Range",
        ip_version=IPVersion.ipv4,
        cidr="10.0.2.0/30",
        is_active=True,
    )
    block = IpBlock(
        id=uuid.uuid4(),
        pool_id=pool.id,
        cidr="10.0.2.0/30",
        is_active=True,
    )
    block.pool = pool
    device_ip = IPv4Address(
        id=uuid.uuid4(),
        address="10.0.2.1",
        pool_id=pool.id,
        is_reserved=True,
    )
    assigned_ip = IPv4Address(
        id=uuid.uuid4(),
        address="10.0.2.2",
        pool_id=pool.id,
        is_reserved=False,
    )
    device_ip.assignment = None
    assigned_ip.assignment = IPAssignment(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        ip_version=IPVersion.ipv4,
        ipv4_address_id=assigned_ip.id,
        is_active=True,
    )
    device = NetworkDevice(
        id=uuid.uuid4(),
        name="Aggregation Switch",
        mgmt_ip="10.0.2.1",
        is_active=True,
    )

    db = FakeSession({IPv4Address: [device_ip, assigned_ip], NetworkDevice: [device]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_pools,
        "list",
        lambda **_kwargs: [pool],
    )
    monkeypatch.setattr(
        web_network_ip.network_service.ip_blocks,
        "list",
        lambda **_kwargs: [block],
    )

    state = web_network_ip.build_ip_pools_data(db)
    pool_util = state["pool_utilization"][str(pool.id)]
    block_util = state["block_utilization"][str(block.id)]

    assert pool_util["used"] == 2
    assert pool_util["reserved"] == 0
    assert pool_util["available"] == 0
    assert pool_util["percent"] == 100
    assert block_util["used"] == 2
    assert block_util["reserved"] == 0
    assert block_util["available"] == 0
    assert block_util["percent"] == 100


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

    state = web_network_ip.build_ipv4_block_detail_data(
        db, block_id=str(block.id), limit=10
    )

    assert state is not None
    rows = {row["ip_address"]: row["status"] for row in state["ip_rows"]}
    assert rows["10.20.30.1"] == "assigned"
    assert rows["10.20.30.2"] == "reserved"
    assert state["stats"]["assigned"] == 1
    assert state["stats"]["reserved"] == 1
    assert state["stats"]["available"] == 0


def test_ipv4_address_list_annotates_additional_route_owner(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Routed Range",
        ip_version=IPVersion.ipv4,
        cidr="10.20.40.0/29",
        is_active=True,
    )
    routed_ip = IPv4Address(
        id=uuid.uuid4(),
        address="10.20.40.2",
        pool_id=pool.id,
        is_reserved=True,
    )
    routed_ip.assignment = None
    subscriber_id = uuid.uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        display_name="Routed Customer",
        full_name="Routed Customer",
        email="routed@example.com",
    )
    route = SubscriberAdditionalRoute(
        id=uuid.uuid4(),
        subscriber_id=subscriber_id,
        cidr="10.20.40.0/30",
        prefix_length=30,
        metric=1,
        is_active=True,
    )
    route.__dict__["subscriber"] = subscriber

    db = FakeSession({SubscriberAdditionalRoute: [route]})
    monkeypatch.setattr(
        web_network_ip.network_service.ipv4_addresses,
        "list",
        lambda **_kwargs: [routed_ip],
    )
    monkeypatch.setattr(
        web_network_ip.network_service.ip_pools,
        "list",
        lambda **_kwargs: [pool],
    )

    state = web_network_ip.build_ip_addresses_data(db, ip_version="ipv4")

    owner = state["addresses"][0].additional_route_owner
    assert owner["subscriber_name"] == "Routed Customer"
    assert owner["subscriber_id"] == str(subscriber_id)
    assert owner["cidr"] == "10.20.40.0/30"


def test_ipv4_block_detail_marks_additional_route_hosts(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Routed Detail Range",
        ip_version=IPVersion.ipv4,
        cidr="10.20.41.0/29",
        is_active=True,
    )
    block = IpBlock(
        id=uuid.uuid4(),
        pool_id=pool.id,
        cidr="10.20.41.0/30",
        is_active=True,
    )
    block.pool = pool
    subscriber_id = uuid.uuid4()
    subscriber = SimpleNamespace(
        id=subscriber_id,
        display_name="Block Route Customer",
        full_name="Block Route Customer",
        email="block-route@example.com",
    )
    route = SubscriberAdditionalRoute(
        id=uuid.uuid4(),
        subscriber_id=subscriber_id,
        cidr="10.20.41.0/30",
        prefix_length=30,
        metric=1,
        is_active=True,
    )
    route.__dict__["subscriber"] = subscriber

    db = FakeSession({SubscriberAdditionalRoute: [route]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_blocks,
        "get",
        lambda **_kwargs: block,
    )

    state = web_network_ip.build_ipv4_block_detail_data(
        db, block_id=str(block.id), limit=10
    )

    assert state is not None
    rows = {row["ip_address"]: row for row in state["ip_rows"]}
    assert rows["10.20.41.1"]["status"] == "routed"
    assert rows["10.20.41.1"]["subscriber_name"] == "Block Route Customer"
    assert rows["10.20.41.1"]["service_ref"] == "10.20.41.0/30"
    assert rows["10.20.41.1"]["notes"] == "Additional routed block"


def test_ipv4_block_detail_marks_ont_management_allocation(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Mgmt Range Detail",
        ip_version=IPVersion.ipv4,
        cidr="10.20.31.0/30",
        is_active=True,
    )
    block = IpBlock(
        id=uuid.uuid4(),
        pool_id=pool.id,
        cidr="10.20.31.0/30",
        is_active=True,
    )
    block.pool = pool
    managed = IPv4Address(
        id=uuid.uuid4(),
        address="10.20.31.1",
        pool_id=pool.id,
        is_reserved=True,
        ont_unit_id=uuid.uuid4(),
        allocation_type="management",
    )
    managed.assignment = None

    db = FakeSession({IPv4Address: [managed]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_blocks,
        "get",
        lambda **_kwargs: block,
    )

    state = web_network_ip.build_ipv4_block_detail_data(
        db, block_id=str(block.id), limit=10
    )

    assert state is not None
    rows = {row["ip_address"]: row["status"] for row in state["ip_rows"]}
    assert rows["10.20.31.1"] == "ont_management"
    assert state["stats"]["assigned"] == 1


def test_ipv4_block_detail_marks_network_device_management_ip(monkeypatch):
    pool = IpPool(
        id=uuid.uuid4(),
        name="Device Range Detail",
        ip_version=IPVersion.ipv4,
        cidr="10.20.32.0/30",
        is_active=True,
    )
    block = IpBlock(
        id=uuid.uuid4(),
        pool_id=pool.id,
        cidr="10.20.32.0/30",
        is_active=True,
    )
    block.pool = pool
    address = IPv4Address(
        id=uuid.uuid4(),
        address="10.20.32.1",
        pool_id=pool.id,
        is_reserved=False,
    )
    address.assignment = None
    device = NetworkDevice(
        id=uuid.uuid4(),
        name="Aggregation Switch",
        mgmt_ip="10.20.32.1",
        is_active=True,
    )

    db = FakeSession({IPv4Address: [address], NetworkDevice: [device]})
    monkeypatch.setattr(
        web_network_ip.network_service.ip_blocks,
        "get",
        lambda **_kwargs: block,
    )

    state = web_network_ip.build_ipv4_block_detail_data(
        db, block_id=str(block.id), limit=10
    )

    assert state is not None
    rows = {row["ip_address"]: row for row in state["ip_rows"]}
    assert rows["10.20.32.1"]["status"] == "device"
    assert rows["10.20.32.1"]["device"] == "Aggregation Switch"
    assert rows["10.20.32.1"]["notes"] == "Network device"
    assert state["stats"]["assigned"] == 1


def test_validate_ip_pool_values_rejects_malformed_cidr_and_mismatches():
    base = {"name": "P", "ip_version": "ipv4"}
    # valid passes
    assert (
        web_network_ip.validate_ip_pool_values({**base, "cidr": "10.0.0.0/24"}) is None
    )
    # garbage / out-of-range prefix rejected
    assert web_network_ip.validate_ip_pool_values({**base, "cidr": "garbage"})
    assert web_network_ip.validate_ip_pool_values({**base, "cidr": "10.0.0.0/99"})
    # version mismatch rejected
    assert web_network_ip.validate_ip_pool_values({**base, "cidr": "2001:db8::/64"})
    # bad gateway / DNS rejected; valid ones pass
    assert web_network_ip.validate_ip_pool_values(
        {**base, "cidr": "10.0.0.0/24", "gateway": "not-an-ip"}
    )
    assert (
        web_network_ip.validate_ip_pool_values(
            {
                **base,
                "cidr": "10.0.0.0/24",
                "gateway": "10.0.0.1",
                "dns_primary": "8.8.8.8",
            }
        )
        is None
    )


def test_range_detail_stats_count_assignments_beyond_display_window():
    """Detail stats must reflect the whole CIDR, not just the first `limit` rows.

    An assignment on .14 of a /28 must count even when only 2 rows are displayed.
    """
    pool = IpPool(
        id=uuid.uuid4(),
        name="Big Range",
        ip_version=IPVersion.ipv4,
        cidr="10.30.0.0/28",
        is_active=True,
    )
    high = IPv4Address(
        id=uuid.uuid4(), address="10.30.0.14", pool_id=pool.id, is_reserved=False
    )
    high.assignment = IPAssignment(
        id=uuid.uuid4(),
        subscriber_id=uuid.uuid4(),
        ip_version=IPVersion.ipv4,
        ipv4_address_id=high.id,
        is_active=True,
    )

    db = FakeSession({IPv4Address: [high]})
    result = web_network_ip._build_ipv4_range_rows(
        db, pool=pool, cidr="10.30.0.0/28", limit=2
    )

    assert result is not None
    assert result["row_count"] == 2  # display window unchanged
    assert result["stats"]["assigned"] == 1  # but the off-window assignment counts
    assert result["stats"]["total_usable"] == 14
