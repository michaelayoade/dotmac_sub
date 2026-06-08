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
    splynx_customer_id: int,
    subscriber_data: dict,
) -> bool:
    """Push one subscriber change to the CRM webhook, retrying on failure.

    Args:
        splynx_customer_id: Splynx customer ID (used as the CRM external_id).
        subscriber_data: Subscriber fields in Splynx API shape.

    Returns:
        True on success.

    Raises:
        CrmPushError: On any push failure, to trigger Celery retry.
    """
    from app.services.crm_webhook import push_subscriber_change as _push

    if _push(splynx_customer_id, subscriber_data):
        return True

    raise CrmPushError(
        f"CRM push failed for customer {splynx_customer_id} "
        f"(attempt {self.request.retries + 1}/{MAX_RETRIES + 1})"
    )
