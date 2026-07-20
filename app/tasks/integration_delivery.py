"""Worker adapter for capability-bound integration deliveries."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations.delivery import execute_delivery


@celery_app.task(
    name="app.tasks.integration_delivery.deliver_integration_event",
    bind=True,
    max_retries=20,
)
def deliver_integration_event(self, delivery_id: str) -> dict[str, object]:
    with db_session_adapter.session() as db:
        delivery = execute_delivery(db, delivery_id=UUID(delivery_id))
        state = delivery.state
        next_attempt_at = delivery.next_attempt_at
        db.commit()
    if state == "retryable" and next_attempt_at is not None:
        delay = max(
            1,
            int((next_attempt_at - datetime.now(UTC)).total_seconds()),
        )
        raise self.retry(countdown=delay)
    return {"delivery_id": delivery_id, "state": state}
