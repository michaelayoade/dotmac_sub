"""Celery task for the vendor project-stub relay (Sub → CRM ``projects``).

Transitional Phase 3 glue (doc 20 §4.3 step 6, risk #6). Runs the relay push off
the request thread with retry, mirroring ``crm_ticket_push``: a slow or
unreachable CRM never blocks a project create/update, and a transient outage
retries with exponential backoff instead of dropping the stub.
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.db import task_session
from app.services.crm_client import CRMClientError
from app.services.vendor_project_relay import push_project_stub

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
RETRY_BACKOFF_MAX = 3600


@celery_app.task(
    name="app.tasks.vendor_project_relay.relay_project_stub_to_crm",
    bind=True,
    max_retries=MAX_RETRIES,
    autoretry_for=(CRMClientError,),
    retry_backoff=True,
    retry_backoff_max=RETRY_BACKOFF_MAX,
    retry_jitter=True,
)
def relay_project_stub_to_crm(self, project_id: str) -> str:
    with task_session() as db:
        return push_project_stub(db, project_id)
