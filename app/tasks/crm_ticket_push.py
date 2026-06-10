"""Celery tasks for outbound ticket/comment push (Sub → DotMac Omni CRM)."""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import task_session
from app.services.crm_client import CRMClientError
from app.services.crm_ticket_push import TicketNotLinkedError, push_comment, push_ticket

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
RETRY_BACKOFF_MAX = 3600


@celery_app.task(
    name="app.tasks.crm_ticket_push.push_ticket_to_crm",
    bind=True,
    max_retries=MAX_RETRIES,
    autoretry_for=(CRMClientError,),
    retry_backoff=True,
    retry_backoff_max=RETRY_BACKOFF_MAX,
    retry_jitter=True,
)
def push_ticket_to_crm(self, ticket_id: str) -> str:
    with task_session() as db:
        outcome = push_ticket(db, ticket_id)
    if outcome == "unresolved_subscriber":
        logger.warning(
            "CRM ticket push skipped (subscriber unresolved) ticket=%s", ticket_id
        )
    return outcome


@celery_app.task(
    name="app.tasks.crm_ticket_push.push_comment_to_crm",
    bind=True,
    max_retries=MAX_RETRIES,
    autoretry_for=(CRMClientError, TicketNotLinkedError),
    retry_backoff=True,
    retry_backoff_max=RETRY_BACKOFF_MAX,
    retry_jitter=True,
)
def push_comment_to_crm(self, comment_id: str) -> str:
    with task_session() as db:
        return push_comment(db, comment_id)
