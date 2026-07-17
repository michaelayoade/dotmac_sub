"""AI transport: retries, the circuit breaker, fallback, and credentials.

Never touches a real provider — httpx is faked at the module boundary.

The credential tests are the load-bearing ones. ``AI_SOT.md`` requires
provider keys to resolve through OpenBao rather than the environment, and a
secret that leaks into a log is a real incident, so both the happy path and
the failure path are pinned here.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.services.ai import client as client_module
from app.services.ai import gateway as gateway_module
from app.services.ai import security as security_module
from app.services.ai.client import AIClientError, VllmClient
from app.services.ai.gateway import AIGateway
from app.services.ai.redaction import redact_text

_URL = "http://ai.local/v1"

_SETTINGS: dict[str, object] = {
    "vllm_label": "primary",
    "vllm_base_url": _URL,
    "vllm_model": "model-a",
    "vllm_api_key": None,
    "vllm_require_api_key": False,
    "vllm_timeout_seconds": 5,
    "vllm_max_retries": 0,
    "vllm_max_tokens": 100,
    "vllm_secondary_label": "secondary",
    "vllm_secondary_base_url": "http://ai-backup.local/v1",
    "vllm_secondary_model": "model-b",
    "vllm_secondary_api_key": None,
    "vllm_secondary_require_api_key": False,
    "vllm_secondary_timeout_seconds": 5,
    "vllm_secondary_max_retries": 0,
    "vllm_secondary_max_tokens": 100,
}


def _settings_stub(overrides: dict[str, object] | None = None):
    merged = {**_SETTINGS, **(overrides or {})}
    return lambda db, domain, key: merged.get(key)


class _FakeResponse:
    def __init__(
        self, status_code: int, payload: dict | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers: dict[str, str] = {}
        self.request = httpx.Request("POST", f"{_URL}/chat/completions")

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=self.request, response=self)


def _chat_payload(content: str) -> dict:
    return {
        "model": "model-a",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 11},
    }


def _fake_httpx(script: list) -> type:
    """A stand-in httpx.Client. ``script`` is consumed across reconstructions,
    because _request_json builds a fresh client on every attempt."""
    shared = list(script)

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_exc) -> bool:
            return False

        def request(self, **_kwargs):
            item = shared.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return _Client


# ── client: retries ─────────────────────────────────────────────────────────


def test_transient_failure_retries_then_succeeds():
    """A 500 is transient: retry, and the caller never sees the blip."""
    c = VllmClient(
        api_key=None, model="model-a", base_url=_URL, max_retries=1, timeout_seconds=5
    )
    script = [
        _FakeResponse(500, text="upstream boom"),
        _FakeResponse(200, _chat_payload("ok")),
    ]
    with (
        patch.object(client_module.httpx, "Client", _fake_httpx(script)),
        patch.object(client_module, "sleep", lambda _s: None),
    ):
        result = c.generate("sys", "prompt", max_tokens=50)
    assert result.content == "ok"
    assert result.tokens_in == 7
    assert result.tokens_out == 11


def test_auth_failure_is_not_retried():
    """401 is our misconfiguration, not the provider's blip. Retrying it just
    hammers the provider with a credential it has already rejected."""
    c = VllmClient(
        api_key="k", model="model-a", base_url=_URL, max_retries=3, timeout_seconds=5
    )
    script = [_FakeResponse(401, text="unauthorized")]  # one response only
    with (
        patch.object(client_module.httpx, "Client", _fake_httpx(script)),
        patch.object(client_module, "sleep", lambda _s: None),
    ):
        with pytest.raises(AIClientError) as exc:
            c.generate("sys", "prompt")
    # A second attempt would have raised IndexError popping an empty script.
    assert exc.value.failure_type == "auth"
    assert exc.value.transient is False


# ── gateway: circuit breaker ────────────────────────────────────────────────


def _failing_client(failure_type: str = "provider_5xx"):
    class _C:
        def generate(self, *_a, **_kw):
            raise AIClientError("boom", failure_type=failure_type, transient=True)

    return _C()


def test_circuit_opens_after_the_threshold_and_then_short_circuits():
    gw = AIGateway()
    with (
        patch.object(gateway_module, "_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 2),
        patch.object(gateway_module, "resolve_value", _settings_stub()),
        patch.object(gateway_module, "ai_enabled", lambda _db: True),
        patch.object(gw, "_client_for", lambda _cfg: _failing_client()),
    ):
        for _ in range(2):
            with pytest.raises(AIClientError) as exc:
                gw.generate(None, endpoint="primary", system="s", prompt="p")
            assert exc.value.failure_type == "provider_5xx"

        # Threshold reached: the next call is refused without touching the provider.
        with pytest.raises(AIClientError) as exc:
            gw.generate(None, endpoint="primary", system="s", prompt="p")
        assert exc.value.failure_type == "circuit_open"
        assert gw.circuit_state(None, "primary")["is_open"] is True


def test_non_transient_failure_does_not_open_the_circuit():
    """A bad API key fails every call; tripping the breaker on it would hide a
    permanent misconfiguration behind a transient-looking symptom."""
    gw = AIGateway()

    class _AuthFail:
        def generate(self, *_a, **_kw):
            raise AIClientError("nope", failure_type="auth", transient=False)

    with (
        patch.object(gateway_module, "_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 2),
        patch.object(gateway_module, "resolve_value", _settings_stub()),
        patch.object(gateway_module, "ai_enabled", lambda _db: True),
        patch.object(gw, "_client_for", lambda _cfg: _AuthFail()),
    ):
        for _ in range(3):
            with pytest.raises(AIClientError):
                gw.generate(None, endpoint="primary", system="s", prompt="p")
        assert gw.circuit_state(None, "primary")["is_open"] is False


# ── gateway: fallback ───────────────────────────────────────────────────────


def test_primary_failure_falls_back_to_secondary():
    gw = AIGateway()

    class _Ok:
        def generate(self, *_a, **_kw):
            return client_module.AIResponse(
                content="from-secondary",
                tokens_in=1,
                tokens_out=2,
                model="model-b",
                provider="secondary",
            )

    def _client_for(cfg):
        return _failing_client() if cfg.label == "primary" else _Ok()

    with (
        patch.object(gateway_module, "resolve_value", _settings_stub()),
        patch.object(gateway_module, "ai_enabled", lambda _db: True),
        patch.object(gw, "_client_for", _client_for),
    ):
        result, meta = gw.generate_with_fallback(None, system="s", prompt="p")
    assert result.content == "from-secondary"
    assert meta["fallback_used"] is True
    assert meta["endpoint"] == "secondary"


def test_no_fallback_when_secondary_is_unconfigured():
    """With nowhere to fall back to, the primary's error must surface as-is —
    not be masked by a second failure."""
    gw = AIGateway()
    stub = _settings_stub(
        {"vllm_secondary_base_url": None, "vllm_secondary_model": None}
    )
    with (
        patch.object(gateway_module, "resolve_value", stub),
        patch.object(gateway_module, "ai_enabled", lambda _db: True),
        patch.object(gw, "_client_for", lambda _cfg: _failing_client()),
    ):
        with pytest.raises(AIClientError) as exc:
            gw.generate_with_fallback(None, system="s", prompt="p")
    assert exc.value.failure_type == "provider_5xx"


# ── inert when unconfigured ─────────────────────────────────────────────────


def test_unconfigured_endpoint_is_not_ready_and_does_not_crash():
    gw = AIGateway()
    stub = _settings_stub({"vllm_base_url": None, "vllm_model": None})
    with (
        patch.object(gateway_module, "resolve_value", stub),
        patch.object(gateway_module, "ai_enabled", lambda _db: True),
    ):
        assert gw.endpoint_ready(None, "primary") is False
        with pytest.raises(AIClientError) as exc:
            gw.generate(None, endpoint="primary", system="s", prompt="p")
    assert "not configured" in str(exc.value)


def test_disabled_ai_refuses_before_reading_any_provider_config():
    gw = AIGateway()
    with patch.object(gateway_module, "ai_enabled", lambda _db: False):
        with pytest.raises(AIClientError) as exc:
            gw.generate(None, endpoint="primary", system="s", prompt="p")
    assert exc.value.failure_type == "ai_disabled"


def test_endpoint_requiring_a_key_is_not_ready_without_one():
    gw = AIGateway()
    stub = _settings_stub({"vllm_require_api_key": True, "vllm_api_key": None})
    with (
        patch.object(gateway_module, "resolve_value", stub),
        patch.object(gateway_module, "ai_enabled", lambda _db: True),
    ):
        assert gw.endpoint_ready(None, "primary") is False


# ── credentials: OpenBao ────────────────────────────────────────────────────


def test_openbao_reference_resolves_to_the_real_key():
    ref = "bao://secret/ai#vllm_key"
    with patch.object(
        security_module, "resolve_secret", lambda v: "sk-real-key" if v == ref else v
    ):
        assert (
            security_module.resolve_provider_api_key(configured_api_key=ref)
            == "sk-real-key"
        )


def test_plaintext_key_passes_through():
    with patch.object(security_module, "resolve_secret", lambda v: v):
        assert (
            security_module.resolve_provider_api_key(configured_api_key="sk-plain")
            == "sk-plain"
        )


def test_unresolvable_reference_fails_closed_without_leaking(caplog):
    """OpenBao down must not hand the caller a broken key, crash the request,
    or write the reference into the log."""

    def _boom(_value):
        raise RuntimeError("bao://secret/ai#vllm_key is unreachable")

    with patch.object(security_module, "resolve_secret", _boom):
        assert (
            security_module.resolve_provider_api_key(
                configured_api_key="bao://secret/ai#k"
            )
            is None
        )
    assert "bao://secret/ai#k" not in caplog.text
    assert "unreachable" not in caplog.text


def test_empty_key_is_none():
    with patch.object(security_module, "resolve_secret", lambda v: v):
        assert (
            security_module.resolve_provider_api_key(configured_api_key="   ") is None
        )
        assert security_module.resolve_provider_api_key(configured_api_key=None) is None


# ── redaction ───────────────────────────────────────────────────────────────


def test_redaction_strips_identifiers_before_the_prompt_leaves():
    raw = "Contact ada@example.com or +2348031234567 about sk-abcdef123456789"
    out = redact_text(raw)
    assert "ada@example.com" not in out
    assert "+2348031234567" not in out
    assert "[redacted-email]" in out
    assert "[redacted-phone]" in out


def test_redact_secret_text_masks_bearer_and_sk_tokens():
    out = security_module.redact_secret_text(
        "authorization: Bearer abc.def-ghi and key sk-abcdefghijklmnop"
    )
    assert "abc.def-ghi" not in out
    assert "sk-abcdefghijklmnop" not in out
    assert "<redacted>" in out
