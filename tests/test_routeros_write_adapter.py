from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.router_management.connection import RouterTransportError
from app.services.router_management.write_adapter import (
    RouterConfigurationWriteAdapter,
    RouterPostWriteReadbackError,
    RouterWriteRejected,
    RouterWriteUnsupported,
    parse_routeros_rest_command,
    redact_router_data,
    verify_routeros_readback,
)


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(RouterWriteUnsupported, match="invalid JSON"):
        parse_routeros_rest_command('/ip/address/add {"address":}')


def test_parse_rejects_unverifiable_action() -> None:
    with pytest.raises(RouterWriteUnsupported, match="not supported"):
        parse_routeros_rest_command('/system/reboot {"delay":"0s"}')


def test_parse_requires_selector_for_remove() -> None:
    with pytest.raises(RouterWriteUnsupported, match="requires one of"):
        parse_routeros_rest_command('/ip/address/remove {"comment":"old"}')


def test_parse_rejects_secret_bearing_generic_write() -> None:
    with pytest.raises(RouterWriteUnsupported, match="Secret-bearing"):
        parse_routeros_rest_command('/user/set {"numbers":"*1","password":"clear"}')


def test_add_readback_matches_desired_fields() -> None:
    plan = parse_routeros_rest_command(
        '/ip/address/add {"address":"192.0.2.1/32","interface":"loopback"}'
    )
    verified, observed, drift = verify_routeros_readback(
        plan,
        [
            {
                ".id": "*7",
                "address": "192.0.2.1/32",
                "interface": "loopback",
                "dynamic": "false",
            }
        ],
    )
    assert verified is True
    assert observed[0][".id"] == "*7"
    assert drift == {}


def test_remove_readback_requires_selector_absent() -> None:
    plan = parse_routeros_rest_command('/ip/address/remove {"numbers":"*7"}')
    verified, observed, drift = verify_routeros_readback(plan, [{".id": "*8"}])
    assert verified is True
    assert observed == []
    assert drift == {}


def test_sensitive_fields_are_redacted() -> None:
    assert redact_router_data(
        {"name": "ops", "password": "clear", "nested": {"api-token": "clear"}}
    ) == {
        "name": "ops",
        "password": "***REDACTED***",
        "nested": {"api-token": "***REDACTED***"},
    }


def test_apply_writes_once_then_reads_back(monkeypatch) -> None:
    calls: list[tuple[str, str, int | None]] = []

    def execute(router, method, path, payload=None, max_retries=None):
        calls.append((method, path, max_retries))
        if method == "POST":
            return {"ret": "*7"}
        return [{".id": "*7", "address": "192.0.2.1/32"}]

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    plan = parse_routeros_rest_command('/ip/address/add {"address":"192.0.2.1/32"}')
    result = RouterConfigurationWriteAdapter().apply(SimpleNamespace(name="r1"), [plan])
    assert result.verified is True
    assert calls == [
        ("POST", "/ip/address/add", 1),
        ("GET", "/ip/address", None),
    ]


def test_transport_failure_becomes_pending_readback(monkeypatch) -> None:
    def execute(*args, **kwargs):
        raise RouterTransportError("timed out")

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    plan = parse_routeros_rest_command('/ip/address/add {"address":"192.0.2.1/32"}')
    with pytest.raises(RouterPostWriteReadbackError, match="outcome is unknown"):
        RouterConfigurationWriteAdapter().apply(SimpleNamespace(name="r1"), [plan])


def test_later_rejection_preserves_verified_partial_result(monkeypatch) -> None:
    calls = 0

    def execute(router, method, path, payload=None, max_retries=None):
        nonlocal calls
        calls += 1
        if path in {"/ip/address/add", "/ip/address"}:
            return {} if method == "POST" else [{"address": "192.0.2.1/32"}]
        raise RuntimeError("not allowed")

    monkeypatch.setattr(
        "app.services.router_management.write_adapter.RouterConnectionService.execute",
        execute,
    )
    plans = [
        parse_routeros_rest_command('/ip/address/add {"address":"192.0.2.1/32"}'),
        parse_routeros_rest_command('/queue/simple/add {"name":"customer-1"}'),
    ]
    with pytest.raises(RouterWriteRejected) as exc_info:
        RouterConfigurationWriteAdapter().apply(SimpleNamespace(name="r1"), plans)

    assert calls == 3
    partial = exc_info.value.partial_result
    assert partial is not None
    assert partial.verified is True
    assert len(partial.commands) == 1
