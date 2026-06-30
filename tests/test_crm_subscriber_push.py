"""Outbound subscriber-change push to the CRM HMAC webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services import crm_webhook

SECRET = "shared-webhook-secret"
BASE = "https://crm.example.test"


@contextmanager
def _settings(secret=SECRET, base=BASE):
    orig_s = settings.crm_webhook_secret
    orig_b = settings.crm_base_url
    object.__setattr__(settings, "crm_webhook_secret", secret)
    object.__setattr__(settings, "crm_base_url", base)
    try:
        yield
    finally:
        object.__setattr__(settings, "crm_webhook_secret", orig_s)
        object.__setattr__(settings, "crm_base_url", orig_b)


def _resp(status_code=200, body=None):
    r = MagicMock()
    r.status_code = status_code
    payload = {"subscriber_id": "crm-uuid-1"} if body is None else body
    r.json.return_value = payload
    r.text = json.dumps(payload)
    return r


def test_push_signs_body_and_posts_to_hmac_webhook():
    with _settings(), patch.object(crm_webhook, "post", return_value=_resp()) as post:
        result = crm_webhook.push_subscriber_change(
            10291, {"balance": "10.00", "currency": "NGN"}, external_system="splynx"
        )

    assert result == "crm-uuid-1"
    args, kwargs = post.call_args
    # Posts to the HMAC public webhook, not the user-authed admin endpoint.
    assert args[0] == f"{BASE}/webhooks/crm/subscribers/sync"
    assert "Authorization" not in kwargs["headers"]

    body = kwargs["data"]
    # external_system travels in the body so CRM keys it correctly.
    assert json.loads(body)["external_system"] == "splynx"
    # Signature is HMAC-SHA256 over the exact bytes posted.
    expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert kwargs["headers"]["X-Selfcare-Signature"] == expected


def test_push_noop_without_secret():
    with _settings(secret=""), patch.object(crm_webhook, "post") as post:
        assert (
            crm_webhook.push_subscriber_change(1, {}, external_system="dotmac") is None
        )
        post.assert_not_called()


def test_push_returns_ok_when_response_has_no_id():
    with _settings(), patch.object(crm_webhook, "post", return_value=_resp(body={})):
        assert (
            crm_webhook.push_subscriber_change(1, {}, external_system="dotmac") == "ok"
        )


def test_push_returns_none_on_failure_status():
    with (
        _settings(),
        patch.object(crm_webhook, "post", return_value=_resp(status_code=401)),
    ):
        assert (
            crm_webhook.push_subscriber_change(1, {}, external_system="splynx") is None
        )
