"""Runtime reader: GET allowlist, failure classification, bounds, optics POST."""

from __future__ import annotations

import ssl
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services.router_health import runtime_reader as rr
from app.services.router_health.contracts import RestFailure

_ROUTER_ID = uuid.uuid4()


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://192.0.2.10/rest/system/resource")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


class _FakeTransport:
    """Record every call; answer from ``responses`` keyed by path."""

    def __init__(self, responses=None, errors=None) -> None:
        self.responses = responses or {}
        self.errors = errors or {}
        self.calls: list[dict] = []

    def __call__(self, router, **kwargs):
        self.calls.append(kwargs)
        path = kwargs["path"]
        if path in self.errors:
            raise self.errors[path]
        return self.responses[path] if path in self.responses else []


_GOOD = {
    rr.PATH_RESOURCE: {"uptime": "1d", "version": "7.16.2", "board-name": "x"},
    rr.PATH_CLOCK: {"date": "2026-10-13", "time": "12:00:00", "gmt-offset": "+00:00"},
    rr.PATH_NTP: {"enabled": "true", "status": "synchronized"},
    rr.PATH_BGP_CONNECTIONS: [{"name": "a", "remote.address": "198.51.100.1"}],
    rr.PATH_BGP_SESSIONS: [{"remote.address": "198.51.100.1", "established": "true"}],
    rr.PATH_OSPF: [],
    rr.PATH_INTERFACES: [
        {"name": "ether1", "type": "ether", "link-downs": 3, "disabled": "false"},
        {"name": "sfp-sfpplus1", "type": "ether", "disabled": "false", "link-downs": 0},
    ],
    rr.PATH_PPP_ACTIVE: [{".id": "*1"}, {".id": "*2"}],
    rr.PATH_USERS: [{"name": "u", "group": "read", "address": "", "disabled": "false"}],
}


def _reader(transport, **limits) -> rr.RouterOSRuntimeReader:
    return rr.RouterOSRuntimeReader(
        rr.ReaderLimits(**limits) if limits else None,
        transport=transport,
        clock=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
    )


def _poll(transport, *, include_optics=False, **limits):
    return _reader(transport, **limits).poll(
        SimpleNamespace(), router_id=_ROUTER_ID, include_optics=include_optics
    )


def test_every_request_is_a_get_on_the_allowlist() -> None:
    transport = _FakeTransport(_GOOD)
    obs = _poll(transport)
    assert obs.failure is None
    assert {c["method"] for c in transport.calls} == {"GET"}
    assert {c["path"] for c in transport.calls} == set(rr.GET_PATHS)
    assert len(transport.calls) == 9  # at most 9 requests per router


def test_every_path_carries_a_proplist_and_ppp_requests_no_usernames() -> None:
    for path in rr.GET_PATHS:
        assert ".proplist=" in path, path
    assert rr.PATH_PPP_ACTIVE.endswith(".proplist=.id")
    assert "name" not in rr.PATH_PPP_ACTIVE
    obs = _poll(_FakeTransport(_GOOD))
    assert obs.ppp_active_count == 2  # a count only


def test_values_are_text_and_collections_are_populated() -> None:
    obs = _poll(_FakeTransport(_GOOD))
    assert obs.resource and obs.resource["uptime"] == "1d"
    assert obs.interfaces and obs.interfaces[0]["link-downs"] == "3"
    assert obs.bgp_connections and obs.users
    assert obs.server_time_utc is not None
    assert obs.optics is None  # not requested


def test_the_reader_refuses_non_allowlisted_requests() -> None:
    instance = _reader(_FakeTransport(_GOOD))
    with pytest.raises(ValueError):
        instance._request(SimpleNamespace(), "GET", "/system/reboot", 1e12)
    with pytest.raises(ValueError):
        instance._request(SimpleNamespace(), "POST", "/system/reboot", 1e12)
    with pytest.raises(ValueError):
        instance._request(SimpleNamespace(), "PUT", rr.PATH_RESOURCE, 1e12)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_status_error(401), RestFailure.auth_failed),
        (_status_error(403), RestFailure.forbidden),
        (httpx.ConnectTimeout("t"), RestFailure.unreachable),
        (httpx.ReadTimeout("t"), RestFailure.unreachable),
        (httpx.ConnectError("refused"), RestFailure.unreachable),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad"), RestFailure.tls),
        (ssl.SSLError("handshake"), RestFailure.tls),
        (RuntimeError("tunnel"), RestFailure.other),
    ],
)
def test_first_request_failure_classification(exc, expected) -> None:
    obs = _poll(_FakeTransport(errors={rr.PATH_RESOURCE: exc}))
    assert obs.failure is expected
    assert obs.resource is None and obs.users is None


def test_status_401_is_distinct_from_unreachable() -> None:
    assert _poll(
        _FakeTransport(errors={rr.PATH_RESOURCE: _status_error(401)})
    ).failure is (RestFailure.auth_failed)
    assert (
        _poll(
            _FakeTransport(errors={rr.PATH_RESOURCE: httpx.ConnectTimeout("t")})
        ).failure
        is RestFailure.unreachable
    )


def test_auth_failure_midway_stops_further_requests_and_discards_partials() -> None:
    transport = _FakeTransport(
        _GOOD, errors={rr.PATH_BGP_CONNECTIONS: _status_error(401)}
    )
    obs = _poll(transport)
    assert obs.failure is RestFailure.auth_failed
    assert obs.resource is None  # partial data discarded: only access is judged
    called = [c["path"] for c in transport.calls]
    assert rr.PATH_BGP_SESSIONS not in called
    assert called[-1] == rr.PATH_BGP_CONNECTIONS


def test_unsupported_endpoint_is_unavailable_not_a_router_failure() -> None:
    transport = _FakeTransport(
        _GOOD, errors={rr.PATH_BGP_CONNECTIONS: _status_error(404)}
    )
    obs = _poll(transport)
    assert obs.failure is None
    assert obs.bgp_connections is None
    assert obs.bgp_sessions is not None
    assert obs.users is not None


def test_deadline_stops_remaining_requests_but_keeps_what_was_read() -> None:
    ticks = iter([0.0, 1.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    instance = rr.RouterOSRuntimeReader(
        rr.ReaderLimits(router_deadline=45),
        transport=_FakeTransport(_GOOD),
        clock=lambda: datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        monotonic=lambda: next(ticks),
    )
    obs = instance.poll(SimpleNamespace(), router_id=_ROUTER_ID, include_optics=False)
    assert obs.failure is None
    assert obs.resource is not None
    assert obs.clock is None and obs.users is None  # past the deadline: carried forward


def test_timeouts_are_capped_by_remaining_deadline_and_settings() -> None:
    transport = _FakeTransport(_GOOD)
    _poll(transport, connect_timeout=5, read_timeout=10, router_deadline=45)
    assert all(
        c["connect_timeout"] <= 5 and c["read_timeout"] <= 10 for c in transport.calls
    )


# ------------------------------------------------------------------- optics


def test_optics_is_one_bounded_post_only_when_requested() -> None:
    responses = {
        **_GOOD,
        rr.OPTICS_PATH: [{"name": "sfp-sfpplus1", "sfp-rx-power": "-9.4"}],
    }
    transport = _FakeTransport(responses)
    obs = _poll(transport, include_optics=True)
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == rr.OPTICS_PATH
    assert posts[0]["payload"] == {"numbers": "sfp-sfpplus1", "once": ""}
    assert obs.optics == {"sfp-sfpplus1": -9.4}
    off = _FakeTransport(responses)
    _poll(off, include_optics=False)
    assert all(c["method"] == "GET" for c in off.calls)


def test_optics_is_bounded_to_sfp_interfaces() -> None:
    rows = tuple(
        {"name": f"sfp{i}", "type": "ether", "disabled": "false"} for i in range(40)
    ) + (
        {"name": "ether1", "type": "ether", "disabled": "false"},
        {"name": "sfp99", "type": "ether", "disabled": "true"},
        {"name": "QSFP28-1", "type": "ether", "disabled": "false"},
    )
    names = rr.sfp_interface_names(rows)
    assert len(names) == rr.MAX_OPTICS_INTERFACES
    assert "ether1" not in names and "sfp99" not in names


def test_optics_post_failure_does_not_fail_the_poll() -> None:
    transport = _FakeTransport(_GOOD, errors={rr.OPTICS_PATH: _status_error(400)})
    obs = _poll(transport, include_optics=True)
    assert obs.failure is None
    assert obs.optics is None


def test_no_sfp_interfaces_means_no_post() -> None:
    responses = {
        **_GOOD,
        rr.PATH_INTERFACES: [{"name": "ether1", "type": "ether", "disabled": "false"}],
    }
    transport = _FakeTransport(responses)
    obs = _poll(transport, include_optics=True)
    assert all(c["method"] == "GET" for c in transport.calls)
    assert obs.optics == {}


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"sfp-rx-power": "-8.337"}, -8.337),
        ({"sfp-rx-power": "-12dBm"}, -12.0),
        ({"rx-power": "2.1"}, 2.1),
        ({"sfp-rx-power": ""}, None),
        ({}, None),
    ],
)
def test_rx_power_parsing(row, expected) -> None:
    assert rr.parse_rx_dbm(row) == expected


def test_reader_never_raises_and_stores_no_exception_text() -> None:
    class Boom(Exception):
        pass

    obs = _poll(_FakeTransport(errors={rr.PATH_RESOURCE: Boom("secret-token-xyz")}))
    assert obs.failure is RestFailure.other
    assert "secret-token-xyz" not in repr(obs)


# --------------------------------------- transport failure after first success


def test_read_timeout_after_first_get_keeps_collected_data() -> None:
    transport = _FakeTransport(
        _GOOD, errors={rr.PATH_BGP_CONNECTIONS: httpx.ReadTimeout("t")}
    )
    obs = _poll(transport)
    assert obs.failure is None  # not "unreachable"
    assert obs.resource is not None and obs.clock is not None and obs.ntp is not None
    # The missing sections are unjudged (None) and the poll stopped there.
    assert obs.bgp_connections is None and obs.bgp_sessions is None
    assert obs.ospf_neighbors is None and obs.interfaces is None
    assert obs.ppp_active_count is None and obs.users is None
    called = [c["path"] for c in transport.calls]
    assert called[-1] == rr.PATH_BGP_CONNECTIONS
    assert rr.PATH_BGP_SESSIONS not in called


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("t")])
def test_transport_errors_after_first_success_are_not_access_failures(exc) -> None:
    obs = _poll(_FakeTransport(_GOOD, errors={rr.PATH_USERS: exc}))
    assert obs.failure is None
    assert obs.users is None and obs.interfaces is not None


def test_first_request_transport_failure_is_still_unreachable() -> None:
    obs = _poll(_FakeTransport(errors={rr.PATH_RESOURCE: httpx.ReadTimeout("t")}))
    assert obs.failure is RestFailure.unreachable and obs.resource is None


@pytest.mark.parametrize("code", [401, 403])
def test_credential_failure_after_first_success_still_discards(code) -> None:
    obs = _poll(_FakeTransport(_GOOD, errors={rr.PATH_USERS: _status_error(code)}))
    assert obs.failure in {RestFailure.auth_failed, RestFailure.forbidden}
    assert obs.resource is None


@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("t"), httpx.ConnectError("x"), _status_error(403)]
)
def test_optics_post_failure_keeps_everything_else_and_marks_attempt(exc) -> None:
    obs = _poll(
        _FakeTransport(_GOOD, errors={rr.OPTICS_PATH: exc}), include_optics=True
    )
    assert obs.failure is None
    assert obs.optics is None and obs.optics_attempted is True
    assert obs.users is not None and obs.interfaces is not None


def test_empty_optics_result_still_counts_as_an_attempt() -> None:
    obs = _poll(_FakeTransport(_GOOD), include_optics=True)
    assert obs.optics == {} and obs.optics_attempted is True


def test_optics_not_requested_is_not_an_attempt() -> None:
    assert _poll(_FakeTransport(_GOOD)).optics_attempted is False


def test_interfaces_are_filtered_to_ether_and_sfp_probe_requires_ether() -> None:
    responses = {
        **_GOOD,
        rr.PATH_INTERFACES: [
            {"name": "ether1", "type": "ether", "disabled": "false"},
            {"name": "sfp-vlan", "type": "vlan", "disabled": "false"},
            {"name": "bridge1", "type": "bridge", "disabled": "false"},
        ],
    }
    obs = _poll(_FakeTransport(responses), include_optics=True)
    assert [r["name"] for r in obs.interfaces or ()] == ["ether1"]
    assert obs.optics == {}
    assert (
        rr.sfp_interface_names(
            ({"name": "sfp-vlan", "type": "vlan", "disabled": "false"},)
        )
        == []
    )
