import importlib
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.models.network import (
    OltCard,
    OltCardPort,
    OltShelf,
    OntAssignment,
    OntStatusSource,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
    PonPort,
    WanMode,
)
from app.services.network import olt_snmp_sync as service
from app.services.network import olt_web_topology as topology_service


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeBind:
    def __init__(self, dialect_name="postgresql"):
        self.dialect = SimpleNamespace(name=dialect_name)


class _FakeDbSession:
    def __init__(self, lock_result=True):
        self._bind = _FakeBind()
        self.lock_result = lock_result
        self.executed = []

    def get_bind(self):
        return self._bind

    def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        return _ScalarResult(self.lock_result)


def test_olt_sync_lock_key_is_deterministic_and_positive() -> None:
    key_one = service._olt_sync_lock_key("olt-123")
    key_two = service._olt_sync_lock_key("olt-123")
    other_key = service._olt_sync_lock_key("olt-456")

    assert key_one == key_two
    assert key_one > 0
    assert key_one != other_key


def test_sync_onts_from_olt_snmp_skips_lock_on_sqlite(db_session, monkeypatch) -> None:
    called = {}

    def fake_impl(session, olt_id):
        called["session"] = session
        called["olt_id"] = olt_id
        return True, "ok", {"discovered": 1, "created": 1, "updated": 0}

    monkeypatch.setattr(service, "_sync_onts_from_olt_snmp_impl", fake_impl)

    result = service.sync_onts_from_olt_snmp(db_session, "olt-sqlite")

    assert result == (True, "ok", {"discovered": 1, "created": 1, "updated": 0})
    assert called == {"session": db_session, "olt_id": "olt-sqlite"}


def test_sync_onts_from_olt_snmp_uses_transaction_lock_on_caller_session(
    monkeypatch,
) -> None:
    fake_db = _FakeDbSession(lock_result=True)
    captured = {}

    def fake_impl(session, olt_id):
        captured["session"] = session
        captured["olt_id"] = olt_id
        return True, "ok", {"discovered": 2, "created": 1, "updated": 1}

    monkeypatch.setattr(service, "_sync_onts_from_olt_snmp_impl", fake_impl)

    result = service.sync_onts_from_olt_snmp(fake_db, "olt-postgres")

    assert result == (True, "ok", {"discovered": 2, "created": 1, "updated": 1})
    assert captured == {"session": fake_db, "olt_id": "olt-postgres"}
    assert fake_db.executed == [
        (
            "SELECT pg_try_advisory_xact_lock(:key)",
            {"key": service._olt_sync_lock_key("olt-postgres")},
        )
    ]


def test_sync_onts_from_olt_snmp_returns_busy_when_lock_not_acquired(
    monkeypatch,
) -> None:
    fake_db = _FakeDbSession(lock_result=False)
    called = {"impl": False}

    def fake_impl(_session, _olt_id):
        called["impl"] = True
        return True, "ok", {}

    monkeypatch.setattr(service, "_sync_onts_from_olt_snmp_impl", fake_impl)

    result = service.sync_onts_from_olt_snmp(fake_db, "olt-postgres")

    assert result == (
        False,
        "Another sync is already running for this OLT",
        {"discovered": 0, "created": 0, "updated": 0},
    )
    assert called["impl"] is False


def test_targeted_snmp_sync_updates_effective_status(db_session, monkeypatch) -> None:
    from app.models.network import OLTDevice
    from app.services.network.olt_targeted_sync import (
        sync_authorized_ont_from_olt_snmp,
    )

    olt = OLTDevice(name="Targeted OLT", vendor="Generic")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-TARGETED-STATUS",
        olt_device_id=olt.id,
        external_id="3",
        online_status=OnuOnlineStatus.unknown,
    )
    db_session.add(ont)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.network.olt_targeted_sync.resolve_snmp_target_for_olt",
        lambda _db, _olt: SimpleNamespace(snmp_enabled=True, vendor="Generic"),
    )

    def fake_walk(_linked, oid, **_kwargs):
        if oid.endswith(".1.1.8"):
            return ["iso.1.2.3.4.0.1.2.3 = INTEGER: 1"]
        if oid.endswith(".10.1.2"):
            return ["iso.1.2.3.4.0.1.2.3 = INTEGER: -1950"]
        if oid.endswith(".10.1.3"):
            return ["iso.1.2.3.4.0.1.2.3 = INTEGER: -2050"]
        if oid.endswith(".1.1.9"):
            return ["iso.1.2.3.4.0.1.2.3 = INTEGER: 1000"]
        return ["iso.1.3.6.1.2.1.1.5.0 = STRING: targeted"]

    ok, message, _stats = sync_authorized_ont_from_olt_snmp(
        db_session,
        olt_id=str(olt.id),
        ont_unit_id=str(ont.id),
        fsp="0/1/2",
        ont_id_on_olt=3,
        serial_number=ont.serial_number,
        walk_fn=fake_walk,
    )

    assert ok, message
    db_session.refresh(ont)
    assert ont.online_status == OnuOnlineStatus.online
    assert ont.effective_status == OnuOnlineStatus.online
    assert ont.effective_status_source == OntStatusSource.olt


def test_sync_onts_from_olt_snmp_impl_flushes_before_final_commit(
    monkeypatch,
) -> None:
    calls = []

    class _FakeSession:
        def __init__(self):
            self._bind = _FakeBind("sqlite")

        def get_bind(self):
            return self._bind

        def flush(self):
            calls.append("flush")

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

    fake_db = _FakeSession()

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: None)

    result = service._sync_onts_from_olt_snmp_impl(fake_db, "missing")

    assert result == (
        False,
        "OLT not found",
        {"discovered": 0, "created": 0, "updated": 0},
    )
    assert calls == []


def test_decode_huawei_packed_fsp_is_deterministic() -> None:
    # 4194320384 = 0xFA000000 (base) + 16384 (delta)
    # delta / 256 = 64 -> slot = 2, port = 0 for the canonical Huawei decoder
    assert service._decode_huawei_packed_fsp(4194320384) == "0/2/0"


def test_sync_impl_skips_unknown_snmp_onu_without_creating_inventory(
    monkeypatch,
) -> None:
    class _FakeScalarList:
        def __init__(self, values):
            self._values = values

        def all(self):
            return list(self._values)

        def first(self):
            return self._values[0] if self._values else None

    class _ExplodingSession:
        def __init__(self):
            self._bind = _FakeBind("sqlite")
            self.rollback_called = 0
            self.scalar_calls = 0

        def get_bind(self):
            return self._bind

        def scalars(self, *_args, **_kwargs):
            self.scalar_calls += 1
            return _FakeScalarList([])

        def flush(self):
            return None

        def rollback(self):
            self.rollback_called += 1

        def commit(self):
            return None

        def add(self, _obj):
            return None

    fake_db = _ExplodingSession()
    fake_olt = SimpleNamespace(
        id="olt-1",
        mgmt_ip="10.0.0.1",
        hostname="olt.local",
        name="OLT 1",
        vendor="Huawei",
        model="MA5600",
        tr069_acs_server_id=None,
        snmp_ro_community="enc",
    )
    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.1",
        hostname="olt.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="Huawei",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: fake_olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.4194320384.3 = INTEGER: 1"],
    )
    monkeypatch.setattr(
        service, "_parse_walk_composite", lambda _lines: {"4194320384.3": "1"}
    )

    result = service._sync_onts_from_olt_snmp_impl(fake_db, "olt-1")

    ok, message, stats = result

    assert ok is True
    assert "ONU telemetry sync complete" in message
    assert stats["discovered"] == 1
    assert stats["created"] == 0
    assert stats["updated"] == 0
    assert stats["skipped"] == 1
    assert stats["assignments_created"] == 0
    assert fake_db.rollback_called == 0


def test_sync_impl_leaves_unresolved_topology_unassigned(
    monkeypatch,
) -> None:
    class _FakeScalarList:
        def __init__(self, values):
            self._values = values

        def all(self):
            return list(self._values)

        def first(self):
            return self._values[0] if self._values else None

    class _FakeSession:
        def __init__(self):
            self._bind = _FakeBind("sqlite")
            self.added = []
            self.commit_called = 0

        def get_bind(self):
            return self._bind

        def scalars(self, *_args, **_kwargs):
            return _FakeScalarList([])

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            return None

        def commit(self):
            self.commit_called += 1

        def rollback(self):
            return None

    fake_db = _FakeSession()
    fake_olt = SimpleNamespace(
        id="olt-2",
        mgmt_ip="10.0.0.2",
        hostname="olt2.local",
        name="OLT 2",
        vendor="ZTE",
        model="C300",
        tr069_acs_server_id=None,
        snmp_ro_community="enc",
    )
    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.2",
        hostname="olt2.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="ZTE",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: fake_olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.99.7 = INTEGER: 1"],
    )
    monkeypatch.setattr(service, "_parse_walk_composite", lambda _lines: {"99.7": "1"})
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, stats = service._sync_onts_from_olt_snmp_impl(fake_db, "olt-2")

    discovered_onts = [
        obj for obj in fake_db.added if obj.__class__.__name__ == "OntUnit"
    ]
    assignments = [
        obj for obj in fake_db.added if obj.__class__.__name__ == "OntAssignment"
    ]
    pon_ports = [obj for obj in fake_db.added if obj.__class__.__name__ == "PonPort"]

    assert ok is True
    assert stats["unresolved_topology"] == 1
    assert stats["created"] == 0
    assert stats["skipped"] == 1
    assert stats["assignments_created"] == 0
    assert discovered_onts == []
    assert assignments == []
    assert pon_ports == []


def test_sync_impl_clears_stale_offline_reason_when_status_becomes_unknown(
    db_session, monkeypatch
) -> None:
    ont = OntUnit(
        serial_number="ONT-UNKNOWN-1",
        olt_device_id=None,
        external_id="zte:99.7",
        online_status=OnuOnlineStatus.offline,
        offline_reason=OnuOfflineReason.los,
    )
    db_session.add(ont)
    db_session.commit()

    fake_olt = SimpleNamespace(
        id=ont.olt_device_id,
        mgmt_ip="10.0.0.2",
        hostname="olt2.local",
        name="OLT 2",
        vendor="ZTE",
        model="C300",
        tr069_acs_server_id=None,
        snmp_ro_community="enc",
    )
    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.2",
        hostname="olt2.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="ZTE",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: fake_olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.99.7 = STRING: unknown"],
    )
    monkeypatch.setattr(
        service, "_parse_walk_composite", lambda _lines: {"99.7": "unknown"}
    )
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, _stats = service._sync_onts_from_olt_snmp_impl(db_session, "olt-2")

    db_session.refresh(ont)
    assert ok is True
    assert ont.online_status == OnuOnlineStatus.unknown
    assert ont.offline_reason is None


def test_sync_impl_auto_created_pon_port_keeps_canonical_port_metadata(
    db_session, monkeypatch
) -> None:
    fake_olt = SimpleNamespace(
        id=uuid4(),
        mgmt_ip="10.0.0.3",
        hostname="olt3.local",
        name="OLT 3",
        vendor="Huawei",
        model="MA5600",
        tr069_acs_server_id=None,
        snmp_ro_community="enc",
    )

    from app.models.network import OLTDevice

    olt = OLTDevice(
        id=fake_olt.id,
        name=fake_olt.name,
        mgmt_ip=fake_olt.mgmt_ip,
        hostname=fake_olt.hostname,
        vendor=fake_olt.vendor,
        model=fake_olt.model,
    )
    db_session.add(olt)
    db_session.flush()
    shelf = OltShelf(olt_id=olt.id, shelf_number=0)
    db_session.add(shelf)
    db_session.flush()
    card = OltCard(shelf_id=shelf.id, slot_number=2)
    db_session.add(card)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTCREAL0001",
        olt_device_id=olt.id,
        external_id="huawei:4194320384.3",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()

    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.3",
        hostname="olt3.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="Huawei",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.4194320384.3 = INTEGER: 1"],
    )
    monkeypatch.setattr(
        service, "_parse_walk_composite", lambda _lines: {"4194320384.3": "1"}
    )
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, stats = service._sync_onts_from_olt_snmp_impl(db_session, str(olt.id))

    pon = db_session.scalars(select(PonPort).where(PonPort.olt_id == olt.id)).first()

    assert ok is True
    assert stats["created"] == 0
    assert stats["updated"] == 1
    assert stats["assignments_created"] == 1
    assert pon is not None
    assert pon.name == "0/2/0"
    assert pon.port_number == 0
    assert pon.olt_card_port_id is not None
    card_port = db_session.get(OltCardPort, pon.olt_card_port_id)
    assert card_port is not None
    assert card_port.port_number == 0


def test_sync_impl_realigns_stale_active_assignment_to_scanned_fsp(
    db_session, monkeypatch
) -> None:
    from app.models.network import OLTDevice

    olt = OLTDevice(
        id=uuid4(),
        name="OLT realign",
        mgmt_ip="10.0.0.31",
        hostname="olt-realign.local",
        vendor="Huawei",
        model="MA5600",
    )
    db_session.add(olt)
    db_session.flush()
    stale_pon = PonPort(olt_id=olt.id, name="0/1/1", is_active=True)
    db_session.add(stale_pon)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTCREALIGN1",
        olt_device_id=olt.id,
        external_id="huawei:4194320384.3",
        board="0/1",
        port="1",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=stale_pon.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.31",
        hostname="olt-realign.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="Huawei",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.4194320384.3 = INTEGER: 1"],
    )
    monkeypatch.setattr(
        service, "_parse_walk_composite", lambda _lines: {"4194320384.3": "1"}
    )
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, stats = service._sync_onts_from_olt_snmp_impl(db_session, str(olt.id))

    db_session.refresh(ont)
    db_session.refresh(assignment)
    scanned_pon = db_session.get(PonPort, assignment.pon_port_id)

    assert ok is True
    assert stats["assignments_created"] == 0
    assert stats["assignments_updated"] == 1
    assert ont.board == "0/2"
    assert ont.port == "0"
    assert scanned_pon is not None
    assert scanned_pon.name == "0/2/0"


def test_sync_impl_repairs_existing_pon_port_metadata(db_session, monkeypatch) -> None:
    fake_olt = SimpleNamespace(
        id=uuid4(),
        mgmt_ip="10.0.0.4",
        hostname="olt4.local",
        name="OLT 4",
        vendor="Huawei",
        model="MA5600",
        tr069_acs_server_id=None,
        snmp_ro_community="enc",
    )

    from app.models.network import OLTDevice

    olt = OLTDevice(
        id=fake_olt.id,
        name=fake_olt.name,
        mgmt_ip=fake_olt.mgmt_ip,
        hostname=fake_olt.hostname,
        vendor=fake_olt.vendor,
        model=fake_olt.model,
    )
    db_session.add(olt)
    db_session.flush()
    shelf = OltShelf(olt_id=olt.id, shelf_number=0)
    db_session.add(shelf)
    db_session.flush()
    card = OltCard(shelf_id=shelf.id, slot_number=2)
    db_session.add(card)
    db_session.flush()
    legacy_port = PonPort(
        olt_id=olt.id,
        name="0/2/0",
        port_number=None,
        olt_card_port_id=None,
        is_active=True,
    )
    db_session.add(legacy_port)
    db_session.flush()
    ont = OntUnit(
        serial_number="HWTCREAL0002",
        olt_device_id=olt.id,
        external_id="huawei:4194320384.3",
        is_active=True,
    )
    db_session.add(ont)
    db_session.flush()

    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.4",
        hostname="olt4.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="Huawei",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.4194320384.3 = INTEGER: 1"],
    )
    monkeypatch.setattr(
        service, "_parse_walk_composite", lambda _lines: {"4194320384.3": "1"}
    )
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, _stats = service._sync_onts_from_olt_snmp_impl(db_session, str(olt.id))

    repaired = db_session.get(PonPort, legacy_port.id)

    assert ok is True
    assert repaired is not None
    assert repaired.port_number == 0
    assert repaired.olt_card_port_id is not None


def test_sync_impl_merges_duplicate_rows_for_same_card_port(
    db_session, monkeypatch
) -> None:
    fake_olt = SimpleNamespace(
        id=uuid4(),
        mgmt_ip="10.0.0.5",
        hostname="olt5.local",
        name="OLT 5",
        vendor="Huawei",
        model="MA5600",
        tr069_acs_server_id=None,
        snmp_ro_community="enc",
    )

    from app.models.network import OLTDevice, OntAssignment

    olt = OLTDevice(
        id=fake_olt.id,
        name=fake_olt.name,
        mgmt_ip=fake_olt.mgmt_ip,
        hostname=fake_olt.hostname,
        vendor=fake_olt.vendor,
        model=fake_olt.model,
    )
    db_session.add(olt)
    db_session.flush()
    shelf = OltShelf(olt_id=olt.id, shelf_number=0)
    db_session.add(shelf)
    db_session.flush()
    card = OltCard(shelf_id=shelf.id, slot_number=2)
    db_session.add(card)
    db_session.flush()
    card_port = OltCardPort(
        card_id=card.id, port_number=0, name="0/2/0", is_active=True
    )
    db_session.add(card_port)
    db_session.flush()
    linked_port = PonPort(
        olt_id=olt.id,
        name="legacy-alt-name",
        port_number=0,
        olt_card_port_id=card_port.id,
        is_active=True,
    )
    db_session.add(linked_port)
    db_session.flush()
    duplicate_name_port = PonPort(
        olt_id=olt.id,
        name="0/2/0",
        port_number=0,
        olt_card_port_id=None,
        is_active=True,
    )
    db_session.add(duplicate_name_port)
    db_session.flush()
    ont = OntUnit(
        serial_number="ONT-DUPLICATE-1",
        olt_device_id=olt.id,
        external_id="huawei:4194320384.3",
        is_active=True,
    )
    old_ont = OntUnit(serial_number="ONT-DUPLICATE-OLD", is_active=True)
    db_session.add(ont)
    db_session.add(old_ont)
    db_session.flush()
    active_assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=duplicate_name_port.id,
        active=True,
    )
    historical_assignment = OntAssignment(
        ont_unit_id=old_ont.id,
        pon_port_id=duplicate_name_port.id,
        active=False,
    )
    db_session.add(active_assignment)
    db_session.add(historical_assignment)
    db_session.flush()

    fake_linked = SimpleNamespace(
        mgmt_ip="10.0.0.5",
        hostname="olt5.local",
        snmp_enabled=True,
        snmp_community="enc",
        snmp_version="v2c",
        snmp_port=None,
        vendor="Huawei",
    )

    monkeypatch.setattr(service, "get_olt_or_none", lambda _db, _id: olt)
    monkeypatch.setattr(
        service, "resolve_snmp_target_for_olt", lambda *_a, **_k: fake_linked
    )
    monkeypatch.setattr(
        service,
        "_run_simple_v2c_walk",
        lambda *_a, **_k: ["1.3.6.1.x.4194320384.3 = INTEGER: 1"],
    )
    monkeypatch.setattr(
        service, "_parse_walk_composite", lambda _lines: {"4194320384.3": "1"}
    )
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, _stats = service._sync_onts_from_olt_snmp_impl(db_session, str(olt.id))

    repaired = db_session.get(PonPort, linked_port.id)
    duplicate = db_session.get(PonPort, duplicate_name_port.id)

    assert ok is True
    assert repaired is not None
    assert repaired.name == "0/2/0"
    assert repaired.olt_card_port_id == card_port.id
    assert duplicate is not None
    assert duplicate.is_active is False
    db_session.refresh(active_assignment)
    db_session.refresh(historical_assignment)
    assert active_assignment.pon_port_id == repaired.id
    assert historical_assignment.pon_port_id == duplicate.id


def test_get_device_summary_runtime_persistence_does_not_commit_when_requested(
    db_session, monkeypatch
) -> None:
    ont = OntUnit(serial_number="ONT-RUNTIME-1", is_active=True)
    db_session.add(ont)
    db_session.commit()

    committed = {"count": 0}
    original_commit = db_session.commit

    def counting_commit():
        committed["count"] += 1
        return original_commit()

    monkeypatch.setattr(db_session, "commit", counting_commit)

    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")

    fake_device = {
        "Device": {},
        "DeviceInfo": {"SerialNumber": {"_value": "REALSERIAL1234"}},
    }

    def fake_extract_parameter_value(device, path):
        current = device
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        if isinstance(current, dict) and "_value" in current:
            return current["_value"]
        return current

    monkeypatch.setattr(
        ont_tr069_module,
        "resolve_genieacs",
        lambda _db, _ont: (
            SimpleNamespace(
                get_device=lambda _device_id: fake_device,
                extract_parameter_value=fake_extract_parameter_value,
            ),
            "device-1",
        ),
    )

    result = ont_tr069_module.OntTR069.get_device_summary(
        db_session,
        str(ont.id),
        persist_observed_runtime=False,
    )
    ont_tr069_module.OntTR069._persist_observed_runtime(
        db_session,
        ont,
        result,
        commit=False,
    )

    assert committed["count"] == 0


def test_persist_observed_runtime_does_not_overwrite_desired_wan_config(
    db_session,
) -> None:
    ont_tr069_module = importlib.import_module("app.services.network.ont_tr069")
    ont = OntUnit(
        serial_number="ONT-DESIRED-WAN",
        pppoe_username="desired-user",
        wan_mode=WanMode.pppoe,
        is_active=False,
    )
    db_session.add(ont)
    db_session.flush()
    summary = ont_tr069_module.TR069Summary(
        available=True,
        system={"Serial": "ONT-DESIRED-WAN"},
        wan={
            "Username": "observed-user",
            "Connection Type": "DHCP",
            "Status": "Connected",
            "WAN IP": "192.0.2.10",
        },
    )

    ont_tr069_module.OntTR069._persist_observed_runtime(
        db_session,
        ont,
        summary,
        commit=False,
    )

    assert ont.pppoe_username == "desired-user"
    assert ont.wan_mode == WanMode.pppoe
    assert ont.observed_pppoe_status == "Connected"
    assert ont.observed_wan_ip == "192.0.2.10"


def test_repair_pon_ports_for_olt_repairs_assignment_derived_legacy_port(
    db_session,
) -> None:
    from app.models.network import OLTDevice

    olt = OLTDevice(
        id=uuid4(),
        name="Repair OLT",
        mgmt_ip="10.0.0.6",
        hostname="olt6.local",
        vendor="Huawei",
        model="MA5600",
    )
    db_session.add(olt)
    db_session.flush()
    shelf = OltShelf(olt_id=olt.id, shelf_number=0)
    db_session.add(shelf)
    db_session.flush()
    card = OltCard(shelf_id=shelf.id, slot_number=2)
    db_session.add(card)
    db_session.flush()
    legacy_port = PonPort(
        olt_id=olt.id,
        name="pon-1",
        port_number=1,
        olt_card_port_id=None,
        is_active=True,
    )
    db_session.add(legacy_port)
    db_session.flush()
    ont = OntUnit(serial_number="ONT-REPAIR-1", board="0/2", port="1", is_active=True)
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=legacy_port.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    ok, _msg, stats = topology_service.repair_pon_ports_for_olt(db_session, str(olt.id))

    repaired_ports = list(
        db_session.scalars(select(PonPort).where(PonPort.olt_id == olt.id)).all()
    )
    active_ports = [port for port in repaired_ports if port.is_active]

    assert ok is True
    assert stats["merged"] == 1
    assert stats["unresolved"] == 0
    assert len(active_ports) == 1
    assert active_ports[0].name == "0/2/1"
    assert active_ports[0].olt_card_port_id is not None
    db_session.refresh(assignment)
    assert assignment.pon_port_id == active_ports[0].id


def test_repair_pon_ports_for_olt_reports_unresolved_ports(db_session) -> None:
    from app.models.network import OLTDevice

    olt = OLTDevice(
        id=uuid4(),
        name="Repair OLT Unresolved",
        mgmt_ip="10.0.0.7",
        hostname="olt7.local",
        vendor="Huawei",
        model="MA5600",
    )
    db_session.add(olt)
    db_session.flush()
    unresolved_port = PonPort(
        olt_id=olt.id,
        name="mystery-port",
        port_number=None,
        olt_card_port_id=None,
        is_active=True,
    )
    db_session.add(unresolved_port)
    db_session.commit()

    ok, _msg, stats = topology_service.repair_pon_ports_for_olt(db_session, str(olt.id))

    assert ok is True
    assert stats["scanned"] == 1
    assert stats["repaired"] == 0
    assert stats["merged"] == 0
    assert stats["unresolved"] == 1
    assert stats["unresolved_ports"][0]["pon_port_id"] == str(unresolved_port.id)


def test_repair_pon_ports_for_olt_skips_inactive_ports(db_session) -> None:
    from app.models.network import OLTDevice

    olt = OLTDevice(
        id=uuid4(),
        name="Repair OLT Inactive",
        mgmt_ip="10.0.0.8",
        hostname="olt8.local",
        vendor="Huawei",
        model="MA5600",
    )
    db_session.add(olt)
    db_session.flush()
    inactive_port = PonPort(
        olt_id=olt.id,
        name="0/2/1",
        port_number=1,
        olt_card_port_id=None,
        is_active=False,
    )
    db_session.add(inactive_port)
    db_session.commit()

    ok, _msg, stats = topology_service.repair_pon_ports_for_olt(db_session, str(olt.id))

    db_session.refresh(inactive_port)
    assert ok is True
    assert stats["scanned"] == 1
    assert stats["repaired"] == 0
    assert stats["merged"] == 0
    assert stats["unresolved"] == 0
    assert inactive_port.is_active is False
