"""Flutterwave v4 token lifecycle and webhook verification.

No credentials needed: the IDP is stubbed and the signature is checked against
vectors computed the way Flutterwave documents.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest

from app.services.integrations.connectors.flutterwave_v4 import (
    TOKEN_ENDPOINT,
    FlutterwaveAuthError,
    FlutterwaveTokenManager,
    expected_webhook_signature,
    verify_webhook_signature,
)

CLIENT_ID = "dotmac-client"
CLIENT_SECRET = "flw-client-secret-must-never-leak"
SECRET_HASH = "dashboard-secret-hash"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _idp(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _token_response(token: str = "access-token-1", expires_in: int = 600):  # noqa: S107
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "expires_in": expires_in,
                "token_type": "Bearer",
            },
        )

    handler.calls = []
    return handler


# --- token acquisition ------------------------------------------------------


def test_a_token_is_requested_with_the_client_credentials_grant():
    handler = _token_response()
    manager = FlutterwaveTokenManager(client_override=_idp(handler))

    token = manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    assert token == "access-token-1"
    request = handler.calls[0]
    assert str(request.url) == TOKEN_ENDPOINT
    assert request.method == "POST"
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    assert f"client_id={CLIENT_ID}" in body


def test_a_cached_token_is_reused_rather_than_refetched():
    """A burst of operations must cost one exchange, not one per call."""
    handler = _token_response()
    manager = FlutterwaveTokenManager(client_override=_idp(handler))

    for _ in range(5):
        manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    assert len(handler.calls) == 1


def test_the_token_is_refreshed_before_it_expires_not_after():
    """Refreshing at expiry would race; a 10-minute token refreshes at 9."""
    clock = _FakeClock()
    handler = _token_response(expires_in=600)
    manager = FlutterwaveTokenManager(client_override=_idp(handler), clock=clock)

    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    clock.advance(500)  # still inside the safe window
    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert len(handler.calls) == 1

    clock.advance(60)  # now past lifetime minus the refresh margin
    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert len(handler.calls) == 2


def test_tokens_are_cached_per_client_not_shared():
    handler = _token_response()
    manager = FlutterwaveTokenManager(client_override=_idp(handler))

    manager.token(client_id="client-a", client_secret=CLIENT_SECRET)
    manager.token(client_id="client-b", client_secret=CLIENT_SECRET)

    assert len(handler.calls) == 2


def test_invalidate_forces_the_next_call_to_refetch():
    handler = _token_response()
    manager = FlutterwaveTokenManager(client_override=_idp(handler))

    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    manager.invalidate(client_id=CLIENT_ID)
    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    assert len(handler.calls) == 2


# --- failure handling -------------------------------------------------------


def test_missing_credentials_are_refused_without_calling_the_idp():
    handler = _token_response()
    manager = FlutterwaveTokenManager(client_override=_idp(handler))

    with pytest.raises(FlutterwaveAuthError, match="required"):
        manager.token(client_id="", client_secret=CLIENT_SECRET)
    assert handler.calls == []


def test_rejected_credentials_raise_without_echoing_the_secret():
    """An auth error must not put the client secret into a log or traceback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    manager = FlutterwaveTokenManager(client_override=_idp(handler))
    with pytest.raises(FlutterwaveAuthError) as excinfo:
        manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    assert CLIENT_SECRET not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_a_transport_failure_does_not_echo_the_posted_form():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    manager = FlutterwaveTokenManager(client_override=_idp(handler))
    with pytest.raises(FlutterwaveAuthError) as excinfo:
        manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    assert CLIENT_SECRET not in str(excinfo.value)


def test_a_response_without_a_token_is_an_error_not_an_empty_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 600})

    manager = FlutterwaveTokenManager(client_override=_idp(handler))
    with pytest.raises(FlutterwaveAuthError, match="no access token"):
        manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)


def test_a_missing_expiry_falls_back_to_the_documented_lifetime():
    clock = _FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(request)
        return httpx.Response(200, json={"access_token": "t"})

    handler.calls = []
    manager = FlutterwaveTokenManager(client_override=_idp(handler), clock=clock)

    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    clock.advance(500)
    manager.token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    assert len(handler.calls) == 1  # treated as a 600s token


# --- webhook verification ---------------------------------------------------


def test_the_signature_matches_flutterwaves_documented_computation():
    body = b'{"event":"charge.completed","data":{"id":1}}'
    reference = base64.b64encode(
        hmac.new(SECRET_HASH.encode(), body, hashlib.sha256).digest()
    ).decode()

    assert expected_webhook_signature(secret_hash=SECRET_HASH, body=body) == reference
    assert verify_webhook_signature(
        secret_hash=SECRET_HASH, body=body, signature=reference
    )


def test_a_tampered_body_fails_verification():
    body = b'{"amount":100}'
    signature = expected_webhook_signature(secret_hash=SECRET_HASH, body=body)

    assert not verify_webhook_signature(
        secret_hash=SECRET_HASH, body=b'{"amount":100000}', signature=signature
    )


def test_the_raw_body_is_what_is_signed_not_a_reserialisation():
    """Re-serialising JSON reorders keys and changes whitespace.

    Verification must run on the exact bytes received or valid webhooks would
    be rejected intermittently.
    """
    received = b'{"b":2,"a":1}'
    reserialised = b'{"a": 1, "b": 2}'
    signature = expected_webhook_signature(secret_hash=SECRET_HASH, body=received)

    assert verify_webhook_signature(
        secret_hash=SECRET_HASH, body=received, signature=signature
    )
    assert not verify_webhook_signature(
        secret_hash=SECRET_HASH, body=reserialised, signature=signature
    )


def test_a_v3_style_plain_secret_is_not_accepted_as_a_signature():
    """The v3 shape must not authenticate a v4 webhook.

    v3 sent the secret verbatim; accepting that here would let anyone holding a
    leaked hash forge a webhook without computing an HMAC.
    """
    body = b'{"event":"charge.completed"}'
    assert not verify_webhook_signature(
        secret_hash=SECRET_HASH, body=body, signature=SECRET_HASH
    )


@pytest.mark.parametrize("signature", ["", "not-base64", "AAAA"])
def test_absent_or_malformed_signatures_fail_closed(signature):
    assert not verify_webhook_signature(
        secret_hash=SECRET_HASH, body=b"{}", signature=signature
    )


def test_a_missing_secret_hash_fails_closed_rather_than_matching_everything():
    body = b"{}"
    assert not verify_webhook_signature(secret_hash="", body=body, signature="anything")
