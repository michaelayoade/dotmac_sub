"""Durable Meta projection of a Sub customer-conversion decision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationInstallationState,
)
from app.models.sales import LeadOriginCapture
from app.services.domain_errors import DomainError
from app.services.events.types import Event, EventType
from app.services.integrations.connectors.meta_social_runtime import (
    META_LEAD_CONVERSION_CAPABILITY,
)
from app.services.integrations.delivery import payload_digest
from app.services.integrations.meta_social_capability import (
    META_SOCIAL_CONNECTOR_KEY,
    send_lead_conversion,
)
from app.services.integrations.meta_social_contracts import MetaLeadConversionCommand
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

META_LEAD_CONVERSION_DELIVERY_SCOPE = "integration:deliver-meta-lead-conversion"
_DELIVER_CONVERSION = OwnerCommandDefinition(
    owner="integration.meta_lead_conversion",
    concern="Meta customer-conversion delivery lifecycle",
    name="deliver_meta_lead_conversion",
)


class MetaLeadConversionError(DomainError):
    """A conversion delivery could not safely move through its lifecycle."""


@dataclass(frozen=True, slots=True)
class DeliverMetaLeadConversionCommand:
    context: CommandContext
    delivery_id: UUID


def _error(suffix: str, message: str) -> MetaLeadConversionError:
    return MetaLeadConversionError(
        code=f"integration.meta_lead_conversion.{suffix}",
        message=message,
    )


def stage_conversion_for_event(
    db: Session, *, event: Event
) -> IntegrationDelivery | None:
    supported_event_types = {
        EventType.lead_account_converted,
        EventType.meta_lead_customer_match_reconciled,
    }
    if event.event_type not in supported_event_types:
        return None
    if (
        event.event_type is EventType.meta_lead_customer_match_reconciled
        and event.payload.get("status") != "single_candidate"
    ):
        return None
    lead_id_value = event.payload.get("lead_id")
    if not lead_id_value:
        return None
    try:
        lead_id = UUID(str(lead_id_value))
    except ValueError:
        return None
    origin = db.scalars(
        select(LeadOriginCapture).where(
            LeadOriginCapture.lead_id == lead_id,
            LeadOriginCapture.source_platform == "meta",
        )
    ).one_or_none()
    if origin is None or not origin.source_interaction_id:
        return None
    bindings = list(
        db.scalars(
            select(IntegrationCapabilityBinding)
            .join(IntegrationCapabilityBinding.installation)
            .where(
                IntegrationCapabilityBinding.capability_id
                == META_LEAD_CONVERSION_CAPABILITY,
                IntegrationCapabilityBinding.state
                == IntegrationBindingState.enabled.value,
                IntegrationCapabilityBinding.installation.has(
                    connector_key=META_SOCIAL_CONNECTOR_KEY,
                    state=IntegrationInstallationState.enabled.value,
                ),
            )
        ).all()
    )
    if not bindings:
        return None
    if len(bindings) != 1:
        raise _error(
            "binding_ambiguous",
            "Multiple enabled Meta conversion bindings require operator repair.",
        )
    binding = bindings[0]
    key = f"meta-lead-conversion:{lead_id}:{binding.id}"
    existing = db.scalars(
        select(IntegrationDelivery).where(IntegrationDelivery.idempotency_key == key)
    ).one_or_none()
    if existing is not None:
        return existing
    payload = {
        "lead_id": str(lead_id),
        "leadgen_id": origin.source_interaction_id,
        "converted_at": event.occurred_at.isoformat(),
        "event_id": str(event.event_id),
    }
    delivery = IntegrationDelivery(
        capability_binding_id=binding.id,
        source_event_id=str(event.event_id),
        event_type=event.event_type.value,
        destination_key=f"meta-conversions:{binding.id}",
        idempotency_key=key,
        payload_digest=payload_digest(payload),
        payload_json=payload,
        state="pending",
    )
    db.add(delivery)
    db.flush()
    return delivery


def queue_conversion(delivery: IntegrationDelivery | None) -> None:
    if delivery is None or delivery.state != "pending":
        return
    from app.services.queue_adapter import enqueue_task
    from app.tasks.integration_delivery import deliver_meta_lead_conversion

    enqueue_task(
        deliver_meta_lead_conversion,
        args=[str(delivery.id)],
        correlation_id=f"meta-lead-conversion:{delivery.source_event_id}",
        source="integration.meta_lead_conversion",
    )


def _execute_conversion(db: Session, *, delivery_id: UUID) -> IntegrationDelivery:
    delivery = db.scalars(
        select(IntegrationDelivery)
        .where(IntegrationDelivery.id == delivery_id)
        .with_for_update()
    ).one_or_none()
    if delivery is None:
        raise _error("delivery_not_found", "Meta conversion delivery was not found.")
    if delivery.state in {"delivered", "canceled", "dead_letter"}:
        return delivery
    if delivery.capability_binding.capability_id != META_LEAD_CONVERSION_CAPABILITY:
        raise _error("capability_mismatch", "Delivery is not a Meta lead conversion.")
    payload = dict(delivery.payload_json or {})
    try:
        converted_at = datetime.fromisoformat(str(payload.get("converted_at") or ""))
    except ValueError:
        delivery.state = "dead_letter"
        delivery.error_code = "integration.meta_lead_conversion.payload_invalid"
        delivery.leased_until = None
        delivery.next_attempt_at = None
        db.flush()
        return delivery
    delivery.state = "leased"
    delivery.leased_until = datetime.now(UTC) + timedelta(minutes=2)
    delivery.last_attempt_at = datetime.now(UTC)
    delivery.attempt_count += 1
    db.flush()
    outcome = send_lead_conversion(
        db,
        MetaLeadConversionCommand(
            leadgen_id=str(payload.get("leadgen_id") or ""),
            converted_at=converted_at,
            event_id=str(payload.get("event_id") or ""),
            correlation_id=f"meta-lead-conversion:{delivery.id}",
        ),
    )
    delivery.leased_until = None
    delivery.error_code = outcome.error_code
    if outcome.accepted:
        delivery.state = "delivered"
        delivery.delivered_at = datetime.now(UTC)
        delivery.next_attempt_at = None
    elif outcome.operation_status in {"retryable", "reconciliation_required"}:
        if delivery.attempt_count >= 10:
            delivery.state = "dead_letter"
        else:
            delivery.state = "retryable"
            delivery.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(8 * 60 * 60, 60 * (2 ** (delivery.attempt_count - 1)))
            )
    else:
        delivery.state = "dead_letter"
    db.flush()
    return delivery


def deliver_conversion(
    db: Session, command: DeliverMetaLeadConversionCommand
) -> IntegrationDelivery:
    def operation() -> IntegrationDelivery:
        if command.context.scope != META_LEAD_CONVERSION_DELIVERY_SCOPE:
            raise _error(
                "scope_invalid",
                "Meta conversion delivery requires its own command scope.",
            )
        return _execute_conversion(db, delivery_id=command.delivery_id)

    return execute_owner_command(
        db,
        definition=_DELIVER_CONVERSION,
        context=command.context,
        operation=operation,
    )
