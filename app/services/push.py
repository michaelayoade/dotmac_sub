"""Mobile push (FCM) transport + device-token registration.

Config-gated: when FCM credentials are not configured the send is a safe no-op
(logged, reported as success so the delivery queue doesn't churn). The in-app
notification record is created regardless; only the push *transport* is gated.

To enable, set:
  - FCM_PROJECT_ID
  - FCM_CREDENTIALS_JSON (inline service-account JSON) or
    GOOGLE_APPLICATION_CREDENTIALS (path to the service-account file)
and install google-auth. No code change is needed to "light it up".
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import httpx
from sqlalchemy.orm import Session

from app.models.device_token import DeviceToken

logger = logging.getLogger(__name__)

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


# --- device-token registry -------------------------------------------------


def register_token(
    db: Session, subscriber_id: str, token: str, platform: str | None = None
) -> DeviceToken:
    """Upsert a device token, (re)binding it to this subscriber and activating it."""
    existing = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if existing:
        existing.subscriber_id = subscriber_id
        existing.platform = platform or existing.platform
        existing.is_active = True
        existing.last_seen_at = datetime.now(UTC)
        db.commit()
        return existing
    row = DeviceToken(
        subscriber_id=subscriber_id, token=token, platform=platform, is_active=True
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def unregister_token(db: Session, subscriber_id: str, token: str) -> bool:
    """Deactivate a token for this subscriber (e.g. on logout). Idempotent."""
    row = (
        db.query(DeviceToken)
        .filter(DeviceToken.token == token)
        .filter(DeviceToken.subscriber_id == subscriber_id)
        .first()
    )
    if not row:
        return False
    row.is_active = False
    db.commit()
    return True


def active_tokens(db: Session, subscriber_id: str) -> list[str]:
    rows = (
        db.query(DeviceToken.token)
        .filter(DeviceToken.subscriber_id == subscriber_id)
        .filter(DeviceToken.is_active.is_(True))
        .all()
    )
    return [r[0] for r in rows]


# --- FCM transport (config-gated) ------------------------------------------


def _fcm_config() -> dict | None:
    """Return {'project_id', 'credentials'} when FCM is configured, else None."""
    project_id = os.getenv("FCM_PROJECT_ID")
    creds_json = os.getenv("FCM_CREDENTIALS_JSON")
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not project_id or not (creds_json or creds_path):
        return None
    return {
        "project_id": project_id,
        "credentials_json": creds_json,
        "credentials_path": creds_path,
    }


def _access_token(cfg: dict) -> str | None:
    """Mint a short-lived OAuth2 access token from the service account."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError:
        logger.warning("push: FCM configured but google-auth is not installed")
        return None
    if cfg.get("credentials_json"):
        info = json.loads(cfg["credentials_json"])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_FCM_SCOPE]
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            cfg["credentials_path"], scopes=[_FCM_SCOPE]
        )
    creds.refresh(Request())
    return creds.token


def send_push(
    db: Session,
    subscriber_id: str,
    title: str,
    body: str,
    *,
    data: dict | None = None,
    notification_id: str | None = None,
) -> bool:
    """Send a push to all of a subscriber's active devices.

    Returns True on success OR when there's nothing to do (no tokens / FCM not
    configured) — both are non-error outcomes for the delivery queue. Returns
    False only on a real transport failure (so the queue retries).
    """
    tokens = active_tokens(db, str(subscriber_id))
    if not tokens:
        logger.info("push: no active device tokens for subscriber %s", subscriber_id)
        return True
    cfg = _fcm_config()
    if not cfg:
        logger.info("push: FCM not configured; skipping transport (in-app only)")
        return True
    access_token = _access_token(cfg)
    if not access_token:
        return False

    url = (
        f"https://fcm.googleapis.com/v1/projects/{cfg['project_id']}/messages:send"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    string_data = {k: str(v) for k, v in (data or {}).items()}
    if notification_id:
        string_data["notification_id"] = str(notification_id)

    ok = 0
    for token in tokens:
        payload = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": string_data,
            }
        }
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code == 200:
                ok += 1
            elif resp.status_code in (400, 404):
                # Token is invalid/unregistered — deactivate it so we stop trying.
                _deactivate(db, token)
                logger.info("push: pruned invalid token for %s", subscriber_id)
            else:
                logger.warning("push: FCM %s for %s", resp.status_code, subscriber_id)
        except Exception as exc:
            logger.warning("push: FCM send error for %s: %s", subscriber_id, exc)
    # Success if at least one device accepted (or all tokens were pruned).
    return ok > 0 or not active_tokens(db, str(subscriber_id))


def _deactivate(db: Session, token: str) -> None:
    row = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if row:
        row.is_active = False
        db.commit()
