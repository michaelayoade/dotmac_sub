"""Broker for customer Portal API sessions (RFC #73).

The mobile/web customer is already authenticated to the sub, so the sub asserts
the subscriber's identity to the CRM server-to-server and the CRM mints a
short-lived, scoped portal token. The client then calls the CRM Portal API
directly with that token (direct-to-CRM via a sub-brokered token) — the sub is
not in the data path for portal feature traffic.

Mirrors ``chat_session`` (the live-chat broker): same identity-assertion model,
same CRM base URL, same fail-soft posture toward the CRM being unavailable.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models.subscriber import Subscriber
from app.services.common import coerce_uuid
from app.services.crm_client import CRMClientError, get_crm_client
from app.services.crm_portal import resolve_crm_subscriber_id

# Least-privilege scopes a subscriber portal token carries. Today only the
# Refer & Earn vertical; widen as portal verticals (projects, work orders,
# quotes) land.
SUBSCRIBER_PORTAL_SCOPES = ["referrals:read", "referrals:write"]


def _portal_api_base() -> str:
    """Absolute base URL the client uses to call the CRM Portal API directly."""
    return f"{settings.crm_base_url.rstrip('/')}/api/v1/portal"


def broker_customer_portal_session(db: Session, subscriber_id: str) -> dict:
    """Mint a scoped Portal API token for an authenticated customer."""
    sub = db.get(Subscriber, coerce_uuid(subscriber_id))
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    crm_subscriber_id = resolve_crm_subscriber_id(db, str(sub.id))
    if not crm_subscriber_id:
        # The account isn't linked to a CRM subscriber yet, so there's no
        # subject the CRM can scope a portal token to.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account is not yet linked to the CRM.",
        )

    try:
        data = get_crm_client().create_portal_session(
            crm_subscriber_id=crm_subscriber_id,
            actor="subscriber",
            scopes=SUBSCRIBER_PORTAL_SCOPES,
        )
    except CRMClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Portal service is temporarily unavailable.",
        ) from exc

    token = str(data.get("portal_token") or "")
    expires_at = data.get("expires_at")
    if not token or not expires_at:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Portal service returned an invalid session.",
        )

    return {
        "portal_token": token,
        "expires_at": int(expires_at),
        "api_base": _portal_api_base(),
    }
