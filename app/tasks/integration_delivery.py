"""Worker adapter for capability-bound integration deliveries."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.celery_app import celery_app
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import delivery as integration_delivery
from app.services.owner_commands import CommandContext


@celery_app.task(
    name="app.tasks.integration_delivery.deliver_integration_event",
    bind=True,
    max_retries=20,
)
def deliver_integration_event(self, delivery_id: str) -> dict[str, object]:
    with db_session_adapter.session() as db:
        delivery = integration_delivery.execute_command(
            db,
            lambda: integration_delivery.execute_delivery(
                db,
                delivery_id=UUID(delivery_id),
            ),
        )
        state = delivery.state
        next_attempt_at = delivery.next_attempt_at
    if state == "retryable" and next_attempt_at is not None:
        delay = max(
            1,
            int((next_attempt_at - datetime.now(UTC)).total_seconds()),
        )
        raise self.retry(countdown=delay)
    return {"delivery_id": delivery_id, "state": state}


@celery_app.task(
    name="app.tasks.integration_delivery.deliver_meta_lead_conversion",
    bind=True,
    max_retries=20,
)
def deliver_meta_lead_conversion(self, delivery_id: str) -> dict[str, object]:
    from app.services.integrations import meta_lead_conversion

    with db_session_adapter.session() as db:
        delivery = meta_lead_conversion.deliver_conversion(
            db,
            meta_lead_conversion.DeliverMetaLeadConversionCommand(
                context=CommandContext.system(
                    actor="integration.meta_lead_conversion.worker",
                    scope=meta_lead_conversion.META_LEAD_CONVERSION_DELIVERY_SCOPE,
                    reason="Deliver the exact queued Meta customer conversion",
                    idempotency_key=f"meta-lead-conversion-attempt:{delivery_id}",
                ),
                delivery_id=UUID(delivery_id),
            ),
        )
        state = delivery.state
        next_attempt_at = delivery.next_attempt_at
    if state == "retryable" and next_attempt_at is not None:
        delay = max(1, int((next_attempt_at - datetime.now(UTC)).total_seconds()))
        raise self.retry(countdown=delay)
    return {"delivery_id": delivery_id, "state": state}


@celery_app.task(
    name="app.tasks.integration_delivery.deliver_meta_capi_lead",
    bind=True,
    max_retries=20,
)
def deliver_meta_capi_lead(self, delivery_id: str) -> dict[str, object]:
    from app.services.integrations import meta_capi_lead

    with db_session_adapter.session() as db:
        delivery = meta_capi_lead.deliver_lead(
            db,
            meta_capi_lead.DeliverMetaCapiLeadCommand(
                context=CommandContext.system(
                    actor="integration.meta_capi_lead.worker",
                    scope=meta_capi_lead.META_CAPI_DELIVERY_SCOPE,
                    reason="Deliver the exact queued website Lead to Meta CAPI",
                    idempotency_key=f"meta-capi-lead-attempt:{delivery_id}",
                ),
                delivery_id=UUID(delivery_id),
            ),
        )
        state = delivery.state
        next_attempt_at = delivery.next_attempt_at
    if state == "retryable" and next_attempt_at is not None:
        delay = max(1, int((next_attempt_at - datetime.now(UTC)).total_seconds()))
        raise self.retry(countdown=delay)
    return {"delivery_id": delivery_id, "state": state}


@celery_app.task(name="app.tasks.integration_delivery.redrive_meta_capi_leads")
def redrive_meta_capi_leads() -> dict[str, int]:
    from app.services.integrations import meta_capi_lead
    from app.services.queue_adapter import enqueue_task

    with db_session_adapter.session() as db:
        delivery_ids = meta_capi_lead.due_delivery_ids(db)
    for delivery_id in delivery_ids:
        enqueue_task(
            deliver_meta_capi_lead,
            args=[str(delivery_id)],
            correlation_id=f"meta-capi-redrive:{delivery_id}",
            source="integration.meta_capi_lead.redrive",
        )
    return {"queued": len(delivery_ids)}
