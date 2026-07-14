from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.router_management.connection import RouterTransportError
from app.services.router_management.sot_policy import (
    RouterSotPolicyError,
    parse_routeros_sot_intent,
    parse_routeros_sot_intents,
)
from app.services.router_management.write_adapter import (
    RouterPostWriteReadbackError,
    RouterSotWriteAdapter,
    RouterWriteRejected,
)


def _intent(*, state: str = "present", values: dict | None = None):
    payload = {
        "resource": "simple_queue",
        "key": "subscriber:123",
        "state": state,
        "values": values
        if values is not None
        else {"name": "dotmac-123", "target": "192.0.2.123/32", "max-limit": "20M/20M"},
    }
    return parse_routeros_sot_intent(payload)


def _owned_queue(**overrides):
    row = {
        ".id": "*7",
        "name": "dotmac-123",
        "target": "192.0.2.123/32",
        "max-limit": "20M/20M",
        "comment": "dotmac-sot:subscriber:123",
    }
    row.update(overrides)
    return row


def _queue_read_path() -> str:
    return (
        "/queue/simple?comment=dotmac-sot%3Asubscriber%3A123"
        "&.proplist=.id%2Ccomment%2Cmax-limit%2Cname%2Ctarget"
    )


def test_policy_rejects_unknown_resource() -> None:
    with pytest.raises(RouterSotPolicyError, match="Unsupported managed"):
        parse_routeros_sot_intent({"resource": "system_script", "key": "bad"})


def test_policy_rejects_unknown_or_secret_fields() -> None:
    with pytest.raises(RouterSotPolicyError, match="not managed"):
        parse_routeros_sot_intent(
            {
                "resource": "simple_queue",
                "key": "bad",
                "values": {"name": "q", "target": "192.0.2.1", "on-event": "reboot"},
            }
        )
    with pytest.raises(RouterSotPolicyError, match="Secret-bearing"):
        parse_routeros_sot_intent(
            {
                "resource": "simple_queue",
                "key": "bad",
                "values": {"name": "q", "target": "192.0.2.1", "password": "x"},
            }
        )


def test_policy_requires_complete_present_state_and_unique_ownership() -> None:
    with pytest.raises(RouterSotPolicyError, match="Required fields"):
        parse_routeros_sot_intent(
            {"resource": "simple_queue", "key": "missing", "values": {"name": "q"}}
        )
    row = _intent().to_dict()
    with pytest.raises(RouterSotPolicyError, match="duplicate"):
        parse_routeros_sot_intents([row, row])


def test_present_adds_owned_resource_and_verifies(monkeypatch) -> None:
    calls = []

    def execute(router, method, path, payload=None, max_retries=None):
        calls.append((method, path, payload, max_retries))
        if method == "POST":
            return {"ret": "*7"}
        if len(calls) == 1:
            return []
        return [_owned_queue()]

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    result = RouterSotWriteAdapter().apply(SimpleNamespace(name="r1"), [_intent()])

    assert result.verified is True
    assert result.commands[0].write_action == "add"
    assert calls == [
        ("GET", _queue_read_path(), None, None),
        (
            "POST",
            "/queue/simple/add",
            {
                "name": "dotmac-123",
                "target": "192.0.2.123/32",
                "max-limit": "20M/20M",
                "comment": "dotmac-sot:subscriber:123",
            },
            1,
        ),
        ("GET", _queue_read_path(), None, None),
    ]


def test_present_updates_only_exact_owned_row(monkeypatch) -> None:
    calls = []
    unmanaged = {".id": "*1", "name": "customer", "max-limit": "1M/1M"}

    def execute(router, method, path, payload=None, max_retries=None):
        calls.append((method, path, payload))
        if method == "POST":
            assert payload["numbers"] == "*7"
            return {}
        if any(call[0] == "POST" for call in calls):
            return [unmanaged, _owned_queue()]
        return [unmanaged, _owned_queue(**{"max-limit": "5M/5M"})]

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    result = RouterSotWriteAdapter().apply(SimpleNamespace(name="r1"), [_intent()])

    assert result.verified is True
    assert result.commands[0].write_action == "set"
    assert calls[1][0:2] == ("POST", "/queue/simple/set")
    assert calls[1][2]["comment"] == "dotmac-sot:subscriber:123"


def test_absent_missing_resource_is_verified_noop(monkeypatch) -> None:
    calls = []

    def execute(router, method, path, payload=None, max_retries=None):
        calls.append((method, path))
        return []

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    result = RouterSotWriteAdapter().apply(
        SimpleNamespace(name="r1"), [_intent(state="absent", values={})]
    )

    assert result.verified is True
    assert result.commands[0].write_action == "no_change"
    absent_path = (
        "/queue/simple?comment=dotmac-sot%3Asubscriber%3A123&.proplist=.id%2Ccomment"
    )
    assert calls == [("GET", absent_path), ("GET", absent_path)]


def test_duplicate_ownership_is_rejected_before_write(monkeypatch) -> None:
    calls = []

    def execute(router, method, path, payload=None, max_retries=None):
        calls.append((method, path))
        return [_owned_queue(), _owned_queue(**{".id": "*8"})]

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    with pytest.raises(RouterWriteRejected, match="ambiguous write"):
        RouterSotWriteAdapter().apply(SimpleNamespace(name="r1"), [_intent()])
    assert calls == [("GET", _queue_read_path())]


def test_transport_failure_after_post_is_pending_readback(monkeypatch) -> None:
    calls = 0

    def execute(router, method, path, payload=None, max_retries=None):
        nonlocal calls
        calls += 1
        if method == "POST":
            raise RouterTransportError("timed out")
        return []

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    with pytest.raises(RouterPostWriteReadbackError, match="outcome is unknown"):
        RouterSotWriteAdapter().apply(SimpleNamespace(name="r1"), [_intent()])
    assert calls == 2


def test_readback_batches_owned_rows_by_resource(monkeypatch) -> None:
    first = _intent()
    second = parse_routeros_sot_intent(
        {
            "resource": "simple_queue",
            "key": "subscriber:456",
            "values": {"name": "dotmac-456", "target": "192.0.2.456/32"},
        }
    )
    calls = []

    def execute(router, method, path, payload=None, max_retries=None):
        calls.append((method, path, payload))
        return [
            _owned_queue(),
            {
                ".id": "*8",
                "name": "dotmac-456",
                "target": "192.0.2.456/32",
                "comment": "dotmac-sot:subscriber:456",
            },
        ]

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    result = RouterSotWriteAdapter().readback(
        SimpleNamespace(name="r1"), [first, second]
    )

    assert result.verified is True
    assert calls == [
        (
            "POST",
            "/queue/simple/print",
            {
                ".proplist": [
                    ".id",
                    "comment",
                    "max-limit",
                    "name",
                    "target",
                ],
                ".query": [
                    "comment=dotmac-sot:subscriber:123",
                    "comment=dotmac-sot:subscriber:456",
                    "#|",
                ],
            },
        )
    ]
