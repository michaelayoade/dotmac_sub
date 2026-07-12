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
    _RETRY_MAX_ATTEMPTS,
    _RETRY_MAX_SLEEP,
    CRMClient,
    CRMClientError,
    _retry_delay,
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


def _seq_client(responses):
    """Patch target for httpx.Client: each construction yields the next response.

    ``_request`` builds a fresh ``httpx.Client`` per attempt, so one response per
    construction models the retry loop.
    """
    it = iter(responses)

    def factory(*_a, **_k):
        resp = next(it)
        inner = MagicMock()
        inner.request.return_value = resp
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=inner)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    return factory


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
# Rate-limit / unavailable retry
# ---------------------------------------------------------------------------


class TestRateLimitRetry:
    def test_429_then_success_is_transparent(self):
        c = _client()
        req = httpx.Request("GET", "https://crm.example/api/v1/tickets")
        responses = [
            httpx.Response(429, headers={"Retry-After": "0"}, request=req),
            httpx.Response(200, json={"ok": True}, request=req),
        ]
        with (
            patch("httpx.Client", side_effect=_seq_client(responses)),
            patch("app.services.crm_client.time.sleep") as sleep,
        ):
            out = c._request("GET", "/api/v1/tickets")
        assert out == {"ok": True}
        sleep.assert_called_once()

    def test_exhausts_retries_then_raises(self):
        c = _client()
        req = httpx.Request("GET", "https://crm.example/api/v1/tickets")
        responses = [
            httpx.Response(429, request=req) for _ in range(_RETRY_MAX_ATTEMPTS + 1)
        ]
        with (
            patch("httpx.Client", side_effect=_seq_client(responses)),
            patch("app.services.crm_client.time.sleep") as sleep,
        ):
            with pytest.raises(CRMClientError, match="429"):
                c._request("GET", "/api/v1/tickets")
        assert sleep.call_count == _RETRY_MAX_ATTEMPTS

    def test_non_retry_status_is_not_retried(self):
        c = _client()
        req = httpx.Request("GET", "https://crm.example/api/v1/tickets")
        responses = [httpx.Response(404, request=req)]
        with (
            patch("httpx.Client", side_effect=_seq_client(responses)),
            patch("app.services.crm_client.time.sleep") as sleep,
        ):
            with pytest.raises(CRMClientError, match="404"):
                c._request("GET", "/api/v1/tickets")
        sleep.assert_not_called()

    def test_scheduler_setting_can_disable_retries(self):
        c = CRMClient("https://crm.example", "user", "pass", settings_db=MagicMock())
        c._token = "tok"
        c._token_expires_at = 10**12
        req = httpx.Request("GET", "https://crm.example/api/v1/tickets")
        responses = [httpx.Response(429, request=req)]

        def fake_resolve_value(db, domain, key):
            if key == "crm_retry_max_attempts":
                return 0
            return None

        with (
            patch(
                "app.services.crm_client.resolve_value", side_effect=fake_resolve_value
            ),
            patch("httpx.Client", side_effect=_seq_client(responses)),
            patch("app.services.crm_client.time.sleep") as sleep,
        ):
            with pytest.raises(CRMClientError, match="429"):
                c._request("GET", "/api/v1/tickets")
        sleep.assert_not_called()

    def test_retry_delay_prefers_retry_after_header(self):
        req = httpx.Request("GET", "x://y")
        resp = httpx.Response(429, headers={"Retry-After": "3"}, request=req)
        assert _retry_delay(resp, 0) == 3.0

    def test_retry_delay_backs_off_and_caps(self):
        req = httpx.Request("GET", "x://y")
        resp = httpx.Response(429, request=req)  # no header
        assert _retry_delay(resp, 0) == 0.5
        assert _retry_delay(resp, 1) == 1.0
        assert _retry_delay(resp, 99) == _RETRY_MAX_SLEEP  # clamps
        # Non-numeric Retry-After (HTTP-date) falls back to backoff.
        bad = httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, request=req
        )
        assert _retry_delay(bad, 0) == 0.5


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


