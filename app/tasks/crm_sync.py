"""Celery task for outbound CRM subscriber sync (Sub → DotMac Omni CRM).

Pushes subscriber/subscription status changes to the CRM webhook endpoint
asynchronously with retry. Doing this off the request thread means a slow or
unreachable CRM never blocks user-facing operations (suspend, reactivate, plan
change, cancel), and a transient CRM outage no longer silently drops the update
— the task retries with exponential backoff instead of drifting out of sync.
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
# Celery handles the backoff: 1m, 2m, 4m, ... capped at 1hr between attempts.
RETRY_BACKOFF_MAX = 3600


class CrmPushError(Exception):
    """Raised when an outbound CRM push fails and should be retried."""


@celery_app.task(
    name="app.tasks.crm_sync.push_subscriber_change",
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
) -> bool:
    """Push one subscriber change to the CRM webhook, retrying on failure.

    Args:
        external_id: CRM external_id — the Splynx customer ID for migrated
            subscribers, the local subscriber UUID for native ones.
        subscriber_data: Subscriber fields (Splynx-shaped for splynx, CRM
            column names otherwise).
        external_system: CRM external system the payload is keyed under.

    Returns:
        True on success.

    Raises:
        CrmPushError: On any push failure, to trigger Celery retry.
    """
    from app.services.crm_webhook import NATIVE_EXTERNAL_SYSTEM
    from app.services.crm_webhook import push_subscriber_change as _push

    crm_subscriber_id = _push(external_id, subscriber_data, external_system)
    if crm_subscriber_id:
        if external_system == NATIVE_EXTERNAL_SYSTEM:
            _persist_crm_link(str(external_id), crm_subscriber_id)
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
        if subscriber and not subscriber.crm_subscriber_id:
            subscriber.crm_subscriber_id = crm_uuid
            db.commit()
