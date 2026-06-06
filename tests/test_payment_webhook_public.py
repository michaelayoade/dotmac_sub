"""Inbound payment-provider webhooks must stay publicly reachable.

Paystack/Flutterwave authenticate by HMAC signature, not a user session. If
their routes live on the user-authed billing ``router`` (or main.py stops
mounting ``webhook_router`` with "none" auth), every real provider callback
gets 401 and the dead-letter safety net never fires. These tests pin both the
route placement and the mount auth mode.
"""

from app.api.billing import router, webhook_router
from app.main import _DEFERRED_API_ROUTER_SPECS

_WEBHOOK_PATHS = {
    "/payment-events/paystack",
    "/payment-events/flutterwave",
}


def _paths(api_router):
    return {r.path for r in api_router.routes}


def test_provider_webhooks_live_on_public_router():
    assert _WEBHOOK_PATHS <= _paths(webhook_router)


def test_provider_webhooks_not_on_user_authed_router():
    # The user-authed billing router must not carry the inbound webhooks.
    assert not (_WEBHOOK_PATHS & _paths(router))


def test_webhook_router_mounted_without_user_auth():
    spec = ("app.api.billing", "webhook_router", "api", "none")
    assert spec in _DEFERRED_API_ROUTER_SPECS, (
        "webhook_router must be mounted with 'none' auth so external providers "
        "can reach it"
    )


def test_billing_router_still_user_authed():
    # Guard against accidentally making the whole billing API public.
    assert ("app.api.billing", "router", "api", "user") in _DEFERRED_API_ROUTER_SPECS
