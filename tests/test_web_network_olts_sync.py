from types import SimpleNamespace

from app.services import web_network_olts as service


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


def test_sync_onts_from_olt_snmp_skips_lock_on_sqlite(
    db_session, monkeypatch
) -> None:
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
    assert service._decode_huawei_packed_fsp(4194320384) == "0/1/0"


def test_sync_impl_fails_closed_when_assignment_creation_rolls_back(
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
            if self.scalar_calls == 1:
                return _FakeScalarList([])
            raise RuntimeError("assignment flush boom")

        def flush(self):
            return None

        def rollback(self):
            self.rollback_called += 1

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
    monkeypatch.setattr(service, "_find_linked_network_device", lambda *_a, **_k: fake_linked)
    monkeypatch.setattr(service, "_run_simple_v2c_walk", lambda *_a, **_k: ["1.3.6.1.x.4194320384.3 = INTEGER: 1"])
    monkeypatch.setattr(service, "_parse_walk_composite", lambda _lines: {"4194320384.3": "1"})

    result = service._sync_onts_from_olt_snmp_impl(fake_db, "olt-1")

    assert result == (
        False,
        "Failed to auto-create ONT assignments: assignment flush boom",
        {
            "discovered": 1,
            "created": 1,
            "updated": 0,
            "assignments_created": 0,
            "assignment_errors": 1,
            "tr069_runtime_synced": 0,
            "tr069_runtime_errors": 0,
        },
    )
    assert fake_db.rollback_called == 1


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
    monkeypatch.setattr(service, "_find_linked_network_device", lambda *_a, **_k: fake_linked)
    monkeypatch.setattr(service, "_run_simple_v2c_walk", lambda *_a, **_k: ["1.3.6.1.x.99.7 = INTEGER: 1"])
    monkeypatch.setattr(service, "_parse_walk_composite", lambda _lines: {"99.7": "1"})
    monkeypatch.setattr(service, "emit_event", lambda *_a, **_k: None)

    ok, _msg, stats = service._sync_onts_from_olt_snmp_impl(fake_db, "olt-2")

    discovered_onts = [obj for obj in fake_db.added if obj.__class__.__name__ == "OntUnit"]
    assignments = [obj for obj in fake_db.added if obj.__class__.__name__ == "OntAssignment"]
    pon_ports = [obj for obj in fake_db.added if obj.__class__.__name__ == "PonPort"]

    assert ok is True
    assert stats["unresolved_topology"] == 1
    assert stats["assignments_created"] == 0
    assert len(discovered_onts) == 1
    assert discovered_onts[0].board is None
    assert discovered_onts[0].port is None
    assert discovered_onts[0].name == "ONU unresolved:99.7"
    assert assignments == []
    assert pon_ports == []
