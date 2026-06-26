"""Tests for CRM client resilience: reachability breaker + response cache.

A slow/unreachable CRM otherwise stalls portal pages for `timeout × N accounts`,
and every page load re-fetches live. These cover the two mitigations added to
``app.services.crm_client``.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.services.crm_client import (
    _REACHABILITY_CIRCUIT,
    CRMClient,
    CRMClientError,
)


@pytest.fixture(autouse=True)
def _reset_circuit():
    """Keep the module-global breaker isolated between tests."""
    _REACHABILITY_CIRCUIT.reset()
    yield
    _REACHABILITY_CIRCUIT.reset()


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets = 0

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.sets += 1
        self.store[key] = value


def _client():
    c = CRMClient("https://crm.example", "user", "pass")
    # Pre-seed a valid token so _request() skips the login round-trip.
    c._token = "tok"
    c._token_expires_at = 10**12
    return c


def _raise_connect(*_a, **_k):
    cm = MagicMock()
    cm.__enter__ = MagicMock(side_effect=httpx.ConnectError("connection refused"))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# Reachability circuit breaker
# ---------------------------------------------------------------------------


class TestReachabilityCircuit:
    def test_starts_closed(self):
        assert _REACHABILITY_CIRCUIT.is_open() is False

    def test_trip_and_reset(self):
        _REACHABILITY_CIRCUIT.trip()
        assert _REACHABILITY_CIRCUIT.is_open() is True
        _REACHABILITY_CIRCUIT.reset()
        assert _REACHABILITY_CIRCUIT.is_open() is False

    def test_open_circuit_fast_fails_without_http(self):
        _REACHABILITY_CIRCUIT.trip()
        c = _client()
        with patch("httpx.Client") as http_client:
            with pytest.raises(CRMClientError, match="circuit open"):
                c._request("GET", "/api/v1/tickets")
        http_client.assert_not_called()  # never touched the network

    def test_connection_error_trips_breaker(self):
        c = _client()
        with patch("httpx.Client", side_effect=_raise_connect):
            with pytest.raises(CRMClientError):
                c.list_work_orders(subscriber_id="s1")
        assert _REACHABILITY_CIRCUIT.is_open() is True

    def test_http_status_error_does_not_trip_breaker(self):
        """A 4xx/5xx means CRM is reachable — must not open the breaker."""
        c = _client()
        request = httpx.Request("GET", "https://crm.example/api/v1/work-orders")
        response = httpx.Response(500, request=request)

        def _raise_status(*_a, **_k):
            cm = MagicMock()
            inner = MagicMock()
            inner.request.side_effect = httpx.HTTPStatusError(
                "boom", request=request, response=response
            )
            cm.__enter__ = MagicMock(return_value=inner)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("httpx.Client", side_effect=_raise_status):
            with pytest.raises(CRMClientError):
                c.list_work_orders(subscriber_id="s1")
        assert _REACHABILITY_CIRCUIT.is_open() is False

    def test_breaker_short_circuits_fanout(self):
        """Once tripped, remaining per-account calls fast-fail (no extra HTTP)."""
        c = _client()
        with patch("httpx.Client", side_effect=_raise_connect) as http_client:
            with pytest.raises(CRMClientError):
                c.list_work_orders(subscriber_id="s1")
            first_call_count = http_client.call_count
            # Second account: breaker is open → no new httpx.Client construction.
            with pytest.raises(CRMClientError):
                c.list_work_orders(subscriber_id="s2")
        assert http_client.call_count == first_call_count


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------


class TestResponseCache:
    def test_second_call_served_from_cache(self):
        c = _client()
        fake = _FakeRedis()
        calls = {"n": 0}

        def fake_request(method, path, params=None, json_data=None):
            calls["n"] += 1
            return [{"id": "wo1", "subscriber_id": "s1"}]

        with (
            patch("app.services.session_store.get_session_redis", return_value=fake),
            patch.object(c, "_request", side_effect=fake_request),
        ):
            r1 = c.list_work_orders(subscriber_id="s1")
            r2 = c.list_work_orders(subscriber_id="s1")

        assert r1 == r2
        assert calls["n"] == 1  # upstream hit once; second served from cache
        assert fake.sets == 1

    def test_distinct_params_use_distinct_keys(self):
        c = _client()
        fake = _FakeRedis()
        calls = {"n": 0}

        def fake_request(method, path, params=None, json_data=None):
            calls["n"] += 1
            return []

        with (
            patch("app.services.session_store.get_session_redis", return_value=fake),
            patch.object(c, "_request", side_effect=fake_request),
        ):
            c.list_work_orders(subscriber_id="s1")
            c.list_work_orders(subscriber_id="s2")

        assert calls["n"] == 2  # different subscriber → different cache key

    def test_no_redis_degrades_to_live_request(self):
        c = _client()
        calls = {"n": 0}

        def fake_request(method, path, params=None, json_data=None):
            calls["n"] += 1
            return []

        with (
            patch("app.services.session_store.get_session_redis", return_value=None),
            patch.object(c, "_request", side_effect=fake_request),
        ):
            c.list_work_orders(subscriber_id="s1")
            c.list_work_orders(subscriber_id="s1")

        assert calls["n"] == 2  # no cache → each call goes upstream


# ---------------------------------------------------------------------------
# Widget chat session
# ---------------------------------------------------------------------------


class TestWidgetSession:
    def test_internal_session_falls_back_to_public_widget_flow(self, monkeypatch):
        c = _client()
        monkeypatch.setenv("APP_URL", "https://selfcare.dotmac.io")
        calls = []

        def fake_request(method, path, params=None, json_data=None, headers=None):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "json_data": json_data,
                    "headers": headers,
                }
            )
            if path == "/api/v1/widget/internal/session":
                raise CRMClientError("shadowed route")
            if path == "/api/v1/widget/cfg-123/session":
                assert headers == {"Origin": "https://selfcare.dotmac.io"}
                return {
                    "session_id": "sess-1",
                    "visitor_token": "vt-1",
                    "conversation_id": None,
                }
            if path == "/api/v1/widget/session/sess-1/identify":
                assert headers == {
                    "Origin": "https://selfcare.dotmac.io",
                    "X-Visitor-Token": "vt-1",
                }
                assert json_data == {
                    "email": "cust@example.com",
                    "name": "Cust Omer",
                    "custom_fields": {
                        "surface": "customer",
                        "crm_subscriber_id": "crm-sub-1",
                    },
                }
                return {"session_id": "sess-1", "conversation_id": "conv-1"}
            raise AssertionError(path)

        with patch.object(c, "_request", side_effect=fake_request):
            result = c.create_widget_session(
                config_id="cfg-123",
                email="cust@example.com",
                name="Cust Omer",
                crm_subscriber_id="crm-sub-1",
                metadata={"surface": "customer"},
            )

        assert result["session_id"] == "sess-1"
        assert result["visitor_token"] == "vt-1"
        assert result["conversation_id"] == "conv-1"
        assert [call["path"] for call in calls] == [
            "/api/v1/widget/internal/session",
            "/api/v1/widget/cfg-123/session",
            "/api/v1/widget/session/sess-1/identify",
        ]
