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
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationStatus,
)
from app.schemas.notification import NotificationCreate, PushIntent, PushIntentV1
from app.services.common import coerce_uuid
from app.services.notification import notifications as notification_records
from app.services.operator_tenant import operator_tenant_id

logger = logging.getLogger(__name__)

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_GENERIC_PUSH_TITLE = "Dotmac update"
_GENERIC_PUSH_BODY = "Open the app to view your update."
_PUSH_INTENT_METADATA_KEY = "push_intent"


# --- device-token registry -------------------------------------------------


def register_token(
    db: Session, subscriber_id: str, token: str, platform: str | None = None
) -> DeviceToken:
    """Upsert a device token, (re)binding it to this subscriber and activating it."""
    sid = coerce_uuid(subscriber_id)
    existing = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if existing:
        existing.subscriber_id = sid
        existing.system_user_id = None
        existing.platform = platform or existing.platform
        existing.is_active = True
        existing.last_seen_at = datetime.now(UTC)
        db.commit()
        return existing
    row = DeviceToken(subscriber_id=sid, token=token, platform=platform, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def register_system_user_token(
    db: Session,
    system_user_id: str,
    token: str,
    platform: str | None = None,
    app_version: str | None = None,
) -> DeviceToken:
    """Upsert a field/staff device token and bind it to a SystemUser."""
    uid = coerce_uuid(system_user_id)
    existing = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if existing:
        existing.subscriber_id = None
        existing.system_user_id = uid
        existing.platform = platform or existing.platform
        existing.app_version = app_version or existing.app_version
        existing.is_active = True
        existing.last_seen_at = datetime.now(UTC)
        db.commit()
        return existing
    row = DeviceToken(
        system_user_id=uid,
        token=token,
        platform=platform,
        app_version=app_version,
        is_active=True,
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


def unregister_system_user_token(
    db: Session, system_user_id: str, device_id: str
) -> bool:
    """Deactivate one staff device owned by the given SystemUser."""
    row = (
        db.query(DeviceToken)
        .filter(DeviceToken.id == coerce_uuid(device_id))
        .filter(DeviceToken.system_user_id == coerce_uuid(system_user_id))
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


def active_system_user_tokens(db: Session, system_user_id: str) -> list[str]:
    rows = (
        db.query(DeviceToken.token)
        .filter(DeviceToken.system_user_id == coerce_uuid(system_user_id))
        .filter(DeviceToken.is_active.is_(True))
        .all()
    )
    return [r[0] for r in rows]


def list_system_user_devices(db: Session, system_user_id: str) -> list[DeviceToken]:
    return (
        db.query(DeviceToken)
        .filter(DeviceToken.system_user_id == coerce_uuid(system_user_id))
        .filter(DeviceToken.is_active.is_(True))
        .order_by(DeviceToken.last_seen_at.desc())
        .all()
    )


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


def _fallback_intent(notification_id: str) -> PushIntent:
    return PushIntent(
        intent_code="notification.open",
        subject_kind="notification",
        subject_id=str(notification_id),
    )


def intent_for_notification(notification: Notification) -> PushIntent:
    """Recover the typed intent stored with a queued notification.

    Rows queued before PushIntentV1 deployment safely open the authenticated
    inbox instead of deriving navigation from their subject/body prose.
    """
    raw = (notification.metadata_ or {}).get(_PUSH_INTENT_METADATA_KEY)
    if isinstance(raw, dict):
        try:
            return PushIntent.model_validate(raw)
        except ValueError:
            logger.warning(
                "push: notification %s carries an invalid intent; using inbox",
                notification.id,
            )
    return _fallback_intent(str(notification.id))


def _wire_intent(intent: PushIntent, *, principal_id: str) -> PushIntentV1:
    return PushIntentV1(
        **intent.model_dump(),
        tenant_id=str(operator_tenant_id()),
        principal_id=str(principal_id),
        issued_at=datetime.now(UTC),
    )


def _fcm_payload(*, token: str, intent: PushIntentV1) -> dict:
    """Build the only payload shape allowed to leave Sub for FCM.

    Customer/staff content remains in authenticated Sub records. The display
    text is deliberately generic and data is the closed PushIntentV1 model.
    """
    data = {
        key: str(value)
        for key, value in intent.model_dump(mode="json", exclude_none=True).items()
    }
    return {
        "message": {
            "token": token,
            "notification": {
                "title": _GENERIC_PUSH_TITLE,
                "body": _GENERIC_PUSH_BODY,
            },
            "data": data,
        }
    }


def send_push(
    db: Session,
    subscriber_id: str,
    title: str,
    body: str,
    *,
    intent: PushIntent | None = None,
    notification_id: str | None = None,
) -> bool:
    """Send a push to all of a subscriber's active devices.

    Returns True on success OR when there's nothing to do (no tokens / FCM not
    configured) — both are non-error outcomes for the delivery queue. Returns
    False only on a real transport failure (so the queue retries).
    """
    if notification_id is None:
        queued = notification_records.create_customer_notification(
            db,
            NotificationCreate(
                subscriber_id=coerce_uuid(subscriber_id),
                channel=NotificationChannel.push,
                recipient=str(subscriber_id),
                subject=title,
                body=body,
                event_type="direct.push",
                category="general",
                metadata_={
                    **(
                        {
                            _PUSH_INTENT_METADATA_KEY: intent.model_dump(
                                mode="json", exclude_none=True
                            )
                        }
                        if intent is not None
                        else {}
                    ),
                    "source": "push_service",
                },
            ),
        )
        return queued.status == NotificationStatus.queued

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

    url = f"https://fcm.googleapis.com/v1/projects/{cfg['project_id']}/messages:send"
    headers = {"Authorization": f"Bearer {access_token}"}
    wire_intent = _wire_intent(
        intent or _fallback_intent(notification_id),
        principal_id=str(subscriber_id),
    )

    ok = 0
    for token in tokens:
        payload = _fcm_payload(token=token, intent=wire_intent)
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


def send_push_to_system_user(
    db: Session,
    system_user_id: str,
    title: str,
    body: str,
    *,
    intent: PushIntent | None = None,
    notification_id: str | None = None,
) -> bool:
    """Send a push to all of a staff/system user's active devices."""
    tokens = active_system_user_tokens(db, str(system_user_id))
    if not tokens:
        logger.info("push: no active device tokens for system user %s", system_user_id)
        return True
    cfg = _fcm_config()
    if not cfg:
        logger.info("push: FCM not configured; skipping staff transport")
        return True
    access_token = _access_token(cfg)
    if not access_token:
        return False

    url = f"https://fcm.googleapis.com/v1/projects/{cfg['project_id']}/messages:send"
    headers = {"Authorization": f"Bearer {access_token}"}
    wire_intent = _wire_intent(
        intent or _fallback_intent(notification_id or system_user_id),
        principal_id=str(system_user_id),
    )

    ok = 0
    for token in tokens:
        payload = _fcm_payload(token=token, intent=wire_intent)
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code == 200:
                ok += 1
            elif resp.status_code in (400, 404):
                _deactivate(db, token)
                logger.info("push: pruned invalid staff token for %s", system_user_id)
            else:
                logger.warning(
                    "push: FCM %s for system user %s",
                    resp.status_code,
                    system_user_id,
                )
        except Exception as exc:
            logger.warning(
                "push: FCM send error for system user %s: %s",
                system_user_id,
                exc,
            )
    return ok > 0 or not active_system_user_tokens(db, str(system_user_id))


def _deactivate(db: Session, token: str) -> None:
    # Flush, don't commit: send_push runs inside the caller's transaction
    # (single-entity webhook mirrors, but also the *batched* ticket pull). A
    # commit here would prematurely commit the whole in-flight batch mid-loop,
    # breaking its atomicity. The caller owns the commit; if it rolls back, this
    # best-effort token prune correctly rolls back with it (and re-prunes next time).
    row = db.query(DeviceToken).filter(DeviceToken.token == token).first()
    if row:
        row.is_active = False
        db.flush()
