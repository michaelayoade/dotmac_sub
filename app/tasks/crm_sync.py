"""Celery task for outbound CRM subscriber sync (Sub → DotMac Omni CRM).

Pushes subscriber/subscription status changes to the CRM webhook endpoint
asynchronously with retry. Doing this off the request thread means a slow or
unreachable CRM never blocks user-facing operations (suspend, reactivate, plan
change, cancel), and a transient CRM outage no longer silently drops the update
— the task retries with exponential backoff instead of drifting out of sync.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from celery import Task

from app.celery_app import celery_app

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
# Celery handles the backoff: 1m, 2m, 4m, ... capped at 1hr between attempts.
RETRY_BACKOFF_MAX = 3600


class CrmPushError(Exception):
    """Raised when an outbound CRM push fails and should be retried."""


class CrmPushTask(Task):
    """Base task that records a dead-letter row when retries are exhausted.

    on_failure fires once, after the final retry — that is the moment a CRM
    change would otherwise drift silently. Covers both the event-driven and
    nightly-billing push paths (they share this task).
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ANN001, D102
        try:
            external_id = args[0] if args else kwargs.get("external_id")
            subscriber_data = (
                args[1] if len(args) > 1 else kwargs.get("subscriber_data")
            )
            external_system = (
                args[2] if len(args) > 2 else kwargs.get("external_system", "splynx")
            )
            _record_dead_letter(
                external_id=str(external_id),
                external_system=str(external_system),
                payload=subscriber_data if isinstance(subscriber_data, dict) else None,
                error=str(exc),
                attempts=self.request.retries + 1,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record CRM dead-letter for %s", task_id)


def _redact_sensitive_payload(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return payload
    redacted = dict(payload)
    if "nin" in redacted and redacted["nin"]:
        redacted["nin"] = "<redacted>"
    return redacted


@celery_app.task(
    name="app.tasks.crm_sync.push_subscriber_change",
    base=CrmPushTask,
    bind=True,
    max_retries=MAX_RETRIES,
    autoretry_for=(CrmPushError,),
    retry_backoff=True,
    retry_backoff_max=RETRY_BACKOFF_MAX,
    retry_jitter=True,
)
def push_subscriber_change(
    self,
    external_id: int | str,
    subscriber_data: dict,
    external_system: str = "splynx",
    *,
    billing_snapshot_subscriber_id: str | None = None,
) -> bool:
    """Push one subscriber change to the CRM webhook, retrying on failure.

    Args:
        external_id: CRM external_id — the imported customer ID for migrated
            subscribers, the local subscriber UUID for native ones.
        subscriber_data: Subscriber fields (legacy-shaped for migrated records, CRM
            column names otherwise).
        external_system: CRM external system the payload is keyed under.
        billing_snapshot_subscriber_id: when set, this push carries a billing
            snapshot; on success the snapshot key is stamped on that
            subscriber so the nightly batch won't re-send it. The stamp lives
            here (not in the batch) so a still-unstamped record is naturally
            re-enqueued next run — auto-heal — while a terminal failure is
            recorded in the dead-letter (on_failure).

    Returns:
        True on success.

    Raises:
        CrmPushError: On any push failure, to trigger Celery retry.
    """
    from app.services.crm_webhook import (
        NATIVE_EXTERNAL_SYSTEM,
        SELFCARE_EXTERNAL_SYSTEM,
    )
    from app.services.crm_webhook import push_subscriber_change as _push

    crm_subscriber_id = _push(external_id, subscriber_data, external_system)
    if crm_subscriber_id:
        if external_system in {NATIVE_EXTERNAL_SYSTEM, SELFCARE_EXTERNAL_SYSTEM}:
            _persist_crm_link(str(external_id), crm_subscriber_id)
        if billing_snapshot_subscriber_id:
            _stamp_billing_snapshot(billing_snapshot_subscriber_id, subscriber_data)
        return True

    raise CrmPushError(
        f"CRM push failed for {external_system} {external_id} "
        f"(attempt {self.request.retries + 1}/{MAX_RETRIES + 1})"
    )


def _persist_crm_link(subscriber_id: str, crm_subscriber_id: str) -> None:
    """Store the CRM subscriber UUID returned by a native push."""
    from uuid import UUID

    from app.db import task_session
    from app.models.subscriber import Subscriber

    try:
        crm_uuid = UUID(crm_subscriber_id)
        local_uuid = UUID(subscriber_id)
    except (TypeError, ValueError):
        return
    with task_session() as db:
        subscriber = db.get(Subscriber, local_uuid)
        if not subscriber:
            return
        if not subscriber.crm_subscriber_id:
            subscriber.crm_subscriber_id = crm_uuid
        metadata = dict(subscriber.metadata_ or {})
        metadata["crm_sync"] = {
            "last_success_at": datetime.now(UTC).isoformat(),
            "crm_subscriber_id": str(crm_uuid),
        }
        subscriber.metadata_ = metadata
        db.commit()


def _stamp_billing_snapshot(subscriber_id: str, sent_payload: dict) -> None:
    """Mark the billing snapshot as delivered on the subscriber.

    Mirrors the key the batch (crm_billing_push) compares against, so the
    next run sees it unchanged and skips re-sending. Migrated-record pushes drop
    billing_cycle, so the stored key matches what the batch would build.
    """
    from uuid import UUID

    from app.db import task_session
    from app.models.subscriber import Subscriber

    try:
        local_uuid = UUID(subscriber_id)
    except (TypeError, ValueError):
        return
    with task_session() as db:
        subscriber = db.get(Subscriber, local_uuid)
        if not subscriber:
            return
        metadata = dict(subscriber.metadata_ or {})
        metadata["crm_billing_snapshot"] = sent_payload
        subscriber.metadata_ = metadata
        db.commit()


def _record_dead_letter(
    *,
    external_id: str,
    external_system: str,
    payload: dict | None,
    error: str,
    attempts: int,
) -> None:
    from app.db import task_session
    from app.models.crm_sync_failure import CrmSyncFailure

    with task_session() as db:
        db.add(
            CrmSyncFailure(
                entity="subscriber",
                external_id=external_id,
                external_system=external_system,
                payload=_redact_sensitive_payload(payload),
                error=error[:2000],
                attempts=attempts,
            )
        )
        db.commit()
    logger.error(
        "CRM push dead-lettered: %s %s after %d attempts",
        external_system,
        external_id,
        attempts,
    )


@celery_app.task(name="app.tasks.crm_sync.redrive_crm_dead_letters")
def redrive_crm_dead_letters():
    """Daily re-drive of unresolved CRM push dead-letters."""
    from app.db import task_session
    from app.services import crm_sync_failures

    with task_session() as db:
        return crm_sync_failures.redrive_all(db)
