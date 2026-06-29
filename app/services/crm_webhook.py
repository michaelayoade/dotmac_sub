"""CRM webhook dispatcher — push subscriber changes to DotMac Omni CRM."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

from requests import RequestException, post

from app.config import settings

logger = logging.getLogger(__name__)


def push_subscriber_change(
    external_id: int | str,
    subscriber_data: dict,
    external_system: str = "splynx",
) -> str | None:
    """Push a subscriber change to the CRM sync webhook.

    Args:
        external_id: The CRM external_id — imported customer ID for migrated
            subscribers, the local subscriber UUID for native ones.
        subscriber_data: Subscriber fields. Legacy-shaped for the migrated
            system; CRM Subscriber column names for any other system (the
            CRM's generic handler instantiates its model from the payload
            verbatim, so unknown keys break creation).
        external_system: CRM external system the payload is keyed under
            (carried in the body so the CRM keeps splynx/native records keyed
            correctly).

    Returns:
        The CRM subscriber UUID on success (or "ok" when the response carries
        no id — still truthy), None on failure.
    """
    secret = settings.crm_webhook_secret
    if not secret:
        logger.warning("Cannot push to CRM: no webhook secret configured")
        return None

    payload = {"id": external_id, "external_system": external_system, **subscriber_data}
    # Sign the exact bytes we send: the CRM verifies HMAC over the raw request
    # body, so serialize once and post that buffer (not json=, which re-encodes).
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    try:
        resp = post(
            f"{settings.crm_base_url}/webhooks/crm/subscribers/sync",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Selfcare-Signature": signature,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            logger.debug("CRM webhook OK for %s %s", external_system, external_id)
            try:
                crm_subscriber_id = resp.json().get("subscriber_id")
            except ValueError:
                crm_subscriber_id = None
            return str(crm_subscriber_id) if crm_subscriber_id else "ok"
        logger.warning(
            "CRM webhook failed for %s %s: %d %s",
            external_system,
            external_id,
            resp.status_code,
            resp.text[:200],
        )
    except RequestException as e:
        logger.warning(
            "CRM webhook error for %s %s: %s", external_system, external_id, e
        )
    return None


# Local subscriber statuses → CRM SubscriberStatus values
# (active / suspended / terminated / pending).
_NATIVE_STATUS_MAP = {
    "new": "pending",
    "active": "active",
    "delinquent": "active",
    "suspended": "suspended",
    "blocked": "suspended",
    "disabled": "terminated",
    "canceled": "terminated",
}

NATIVE_EXTERNAL_SYSTEM = "dotmac"


def native_status(status: object) -> str:
    """Map a local subscriber status to the CRM's status vocabulary."""
    value = getattr(status, "value", status)
    return _NATIVE_STATUS_MAP.get(str(value or "").lower(), "pending")


def native_subscriber_payload(
    subscriber,
    service_name: str = "",
    service_speed: str = "",
    status: str | None = None,
) -> dict:
    """Build a generic-webhook payload for a native subscriber.

    Only CRM Subscriber column names: the CRM's generic handler creates its
    model from the payload verbatim. The CRM has no name field on
    subscribers (names hang off person/organization links), so the display
    name goes into notes for agents.
    """
    name = (
        subscriber.display_name
        or f"{subscriber.first_name} {subscriber.last_name}".strip()
    )
    payload = {
        "status": status or native_status(subscriber.status),
        "notes": f"DotMac Sub native subscriber: {name} <{subscriber.email}>",
    }
    if subscriber.subscriber_number:
        payload["subscriber_number"] = subscriber.subscriber_number
    if subscriber.account_number:
        payload["account_number"] = subscriber.account_number
    if service_name:
        payload["service_name"] = service_name
    if service_speed:
        payload["service_speed"] = service_speed
    return payload


def status_change_payload(new_status: str, name: str = "") -> dict:
    """Build the subscriber-status webhook payload (no I/O)."""
    return {
        "status": new_status,
        "name": name,
        "last_update": datetime.now(UTC).isoformat(),
    }


def service_activation_payload(
    service_name: str,
    service_speed: str = "",
    status: str = "active",
) -> dict:
    """Build the service-activation webhook payload (no I/O)."""
    return {
        "status": status,
        "service_name": service_name,
        "service_speed": service_speed,
        "last_update": datetime.now(UTC).isoformat(),
    }
