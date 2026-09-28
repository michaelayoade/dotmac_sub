"""Durable owner for confirmed fiber website Lead delivery to Meta CAPI."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urljoin, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationInstallationState,
)
from app.models.sales import LeadOriginCapture
from app.services.customer_identity_normalization import (
    default_country_code,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.domain_errors import DomainError
from app.services.events.types import Event, EventType
from app.services.integrations.connectors.meta_social_runtime import (
    META_CAPI_CONNECTOR_KEY,
    META_WEBSITE_LEAD_CAPABILITY,
)
from app.services.integrations.delivery import payload_digest
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    RuntimeExecutionError,
    build_execution_context,
    make_operation_executor,
)
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

META_CAPI_STAGE_SCOPE = "integration:stage-meta-capi-fiber-lead"
META_CAPI_DELIVERY_SCOPE = "integration:deliver-meta-capi-fiber-lead"
_STAGE = OwnerCommandDefinition(
    owner="integration.meta_capi_lead",
    concern="Meta website Lead delivery projection",
    name="stage_meta_capi_fiber_lead",
)
_DELIVER = OwnerCommandDefinition(
    owner="integration.meta_capi_lead",
    concern="Meta website Lead delivery lifecycle",
    name="deliver_meta_capi_fiber_lead",
)


class MetaCapiLeadError(DomainError):
    """Stable Meta website Lead lifecycle error."""


class StageOutcome(StrEnum):
    ineligible = "ineligible"
    disabled = "disabled"
    queued = "queued"
    deduplicated = "deduplicated"


@dataclass(frozen=True, slots=True)
class StageResult:
    outcome: StageOutcome
    delivery_id: UUID | None = None
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class StageMetaCapiLeadCommand:
    context: CommandContext
    event: Event


@dataclass(frozen=True, slots=True)
class DeliverMetaCapiLeadCommand:
    context: CommandContext
    delivery_id: UUID


@dataclass(frozen=True, slots=True)
class MetaCapiHealthSnapshot:
    queued: int
    succeeded: int
    failed: int
    retrying: int
    deduplicated: int
    last_success_at: datetime | None


def _error(suffix: str, message: str, **details: object) -> MetaCapiLeadError:
    return MetaCapiLeadError(
        code=f"integration.meta_capi_lead.{suffix}",
        message=message,
        details=details,
        retryable=False,
    )


def normalize_and_hash_email(value: str | None) -> str | None:
    normalized = normalize_email_identifier(value)
    return (
        hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None
    )


def normalize_and_hash_phone(
    value: str | None, *, country_code: str = "234"
) -> str | None:
    normalized = normalize_phone_identifier(value, default_country_code=country_code)
    if not normalized:
        return None
    digits = re.sub(r"\D", "", normalized)
    return hashlib.sha256(digits.encode("utf-8")).hexdigest() if digits else None


def meta_lead_event_id(origin_capture_id: UUID) -> str:
    """Stable cross-channel identifier suitable for future browser deduplication."""

    return str(
        uuid5(NAMESPACE_URL, f"dotmac:meta-capi:website-lead:{origin_capture_id}")
    )


def _source_url(landing_path: str | None) -> str:
    candidate = urljoin("https://fiber.dotmac.ng/", str(landing_path or "/").strip())
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.netloc != "fiber.dotmac.ng":
        return "https://fiber.dotmac.ng/"
    return candidate


def _enabled_binding(db: Session) -> IntegrationCapabilityBinding | None:
    rows = list(
        db.scalars(
            select(IntegrationCapabilityBinding)
            .join(IntegrationCapabilityBinding.installation)
            .where(
                IntegrationCapabilityBinding.capability_id
                == META_WEBSITE_LEAD_CAPABILITY,
                IntegrationCapabilityBinding.state
                == IntegrationBindingState.enabled.value,
                IntegrationCapabilityBinding.installation.has(
                    connector_key=META_CAPI_CONNECTOR_KEY,
                    state=IntegrationInstallationState.enabled.value,
                ),
            )
        ).all()
    )
    if len(rows) > 1:
        raise _error(
            "binding_ambiguous",
            "Multiple enabled Meta CAPI website Lead bindings require repair.",
            binding_count=len(rows),
        )
    return rows[0] if rows else None


def _stage(db: Session, event: Event) -> StageResult:
    if event.event_type is not EventType.lead_created:
        return StageResult(StageOutcome.ineligible)
    raw_origin_id = event.payload.get("origin_capture_id")
    try:
        origin_id = UUID(str(raw_origin_id))
    except (TypeError, ValueError):
        return StageResult(StageOutcome.ineligible)
    origin = db.get(LeadOriginCapture, origin_id)
    if origin is None or origin.integration_inbox is None:
        return StageResult(StageOutcome.ineligible)
    inbox_payload = dict(origin.integration_inbox.payload_json or {})
    if not (
        origin.capture_source == "fiber.website_inquiry"
        and origin.source_platform == "website"
        and origin.external_form_id == "fiber-coverage-v1"
        and str(inbox_payload.get("interest") or "") == "new_connection"
    ):
        return StageResult(StageOutcome.ineligible)
    binding = _enabled_binding(db)
    if binding is None:
        return StageResult(StageOutcome.disabled)

    event_id = meta_lead_event_id(origin.id)
    key = f"meta-capi-website-lead:{origin.id}"
    existing = db.scalar(
        select(IntegrationDelivery).where(IntegrationDelivery.idempotency_key == key)
    )
    if existing is not None:
        from app.metrics import META_CAPI_LEAD_EVENTS

        receipt = dict(existing.external_receipt_json or {})
        receipt["deduplicated_count"] = int(receipt.get("deduplicated_count") or 0) + 1
        existing.external_receipt_json = receipt
        db.flush()
        logger.info(
            "Meta CAPI Lead deduplicated",
            extra={
                "lead_id": str(origin.lead_id),
                "inquiry_id": str(origin.integration_inbox_id),
                "event_id": event_id,
                "delivery_state": existing.state,
            },
        )
        META_CAPI_LEAD_EVENTS.labels(outcome="deduplicated").inc()
        return StageResult(StageOutcome.deduplicated, existing.id, event_id)

    email_hash = normalize_and_hash_email(str(inbox_payload.get("email") or ""))
    phone_hash = normalize_and_hash_phone(
        str(inbox_payload.get("phone") or ""),
        country_code=default_country_code(db),
    )
    user_data = {
        key: [value]
        for key, value in (("em", email_hash), ("ph", phone_hash))
        if value is not None
    }
    if not user_data:
        return StageResult(StageOutcome.ineligible)
    event_time = origin.submitted_at or origin.created_at
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=UTC)
    payload = {
        "lead_id": str(origin.lead_id),
        "inquiry_id": str(origin.integration_inbox_id),
        "origin_capture_id": str(origin.id),
        "event_id": event_id,
        "event_time": int(event_time.timestamp()),
        "event_source_url": _source_url(origin.landing_path),
        "user_data": user_data,
    }
    delivery = IntegrationDelivery(
        capability_binding_id=binding.id,
        source_event_id=str(event.event_id),
        event_type="meta.capi.website_lead",
        destination_key=f"meta-capi:{binding.id}",
        idempotency_key=key,
        payload_digest=payload_digest(payload),
        payload_json=payload,
        state="pending",
    )
    db.add(delivery)
    db.flush()
    from app.metrics import META_CAPI_LEAD_EVENTS

    META_CAPI_LEAD_EVENTS.labels(outcome="queued").inc()
    logger.info(
        "Meta CAPI Lead queued",
        extra={
            "lead_id": str(origin.lead_id),
            "inquiry_id": str(origin.integration_inbox_id),
            "event_id": event_id,
            "delivery_id": str(delivery.id),
            "delivery_state": delivery.state,
        },
    )
    return StageResult(StageOutcome.queued, delivery.id, event_id)


def stage_lead(db: Session, command: StageMetaCapiLeadCommand) -> StageResult:
    def operation() -> StageResult:
        if command.context.scope != META_CAPI_STAGE_SCOPE:
            raise _error(
                "scope_invalid",
                "Meta CAPI Lead staging requires its own command scope.",
            )
        return _stage(db, command.event)

    return execute_owner_command(
        db, definition=_STAGE, context=command.context, operation=operation
    )


def queue_delivery(result: StageResult) -> None:
    if result.outcome is not StageOutcome.queued or result.delivery_id is None:
        return
    from app.services.queue_adapter import enqueue_task
    from app.tasks.integration_delivery import deliver_meta_capi_lead

    enqueue_task(
        deliver_meta_capi_lead,
        args=[str(result.delivery_id)],
        correlation_id=f"meta-capi-lead:{result.event_id}",
        source="integration.meta_capi_lead",
    )


def _deliver(db: Session, delivery_id: UUID) -> IntegrationDelivery:
    delivery = db.scalar(
        select(IntegrationDelivery)
        .where(IntegrationDelivery.id == delivery_id)
        .with_for_update()
    )
    if delivery is None:
        raise _error("delivery_not_found", "Meta CAPI Lead delivery was not found.")
    if delivery.state in {"delivered", "canceled", "dead_letter"}:
        return delivery
    now = datetime.now(UTC)
    if (
        delivery.state == "leased"
        and delivery.leased_until
        and delivery.leased_until > now
    ):
        return delivery
    if delivery.capability_binding.capability_id != META_WEBSITE_LEAD_CAPABILITY:
        raise _error("capability_mismatch", "Delivery is not a Meta website Lead.")

    delivery.state = "leased"
    delivery.leased_until = now + timedelta(minutes=2)
    delivery.last_attempt_at = now
    delivery.attempt_count += 1
    db.flush()
    try:
        context = build_execution_context(
            db, capability_binding_id=delivery.capability_binding_id
        )
        executor = make_operation_executor(
            context,
            correlation_id=f"meta-capi-lead:{delivery.id}",
            trigger=OperationTrigger.event,
            actor="integration.meta_capi_lead",
            timeout_seconds=int(context.config.get("timeout_seconds") or 10) + 5,
        )
        result = executor("send_website_lead", dict(delivery.payload_json or {}))
        max_attempts = max(1, min(int(context.config.get("max_attempts") or 8), 20))
    except RuntimeExecutionError:
        from app.metrics import META_CAPI_LEAD_EVENTS

        delivery.state = "dead_letter"
        delivery.error_code = "configuration_unavailable"
        delivery.error_detail = None
        delivery.leased_until = None
        delivery.next_attempt_at = None
        db.flush()
        META_CAPI_LEAD_EVENTS.labels(outcome="failed").inc()
        logger.error(
            "Meta CAPI Lead configuration unavailable",
            extra={"delivery_id": str(delivery.id), "delivery_state": delivery.state},
        )
        return delivery

    previous = dict(delivery.external_receipt_json or {})
    deduplicated_count = int(previous.get("deduplicated_count") or 0)
    delivery.external_receipt_json = {
        **dict(result.external_receipt),
        **({"deduplicated_count": deduplicated_count} if deduplicated_count else {}),
    }
    delivery.response_status = result.external_receipt.get("response_status")
    delivery.error_code = result.error_code
    delivery.error_detail = None
    delivery.leased_until = None
    if result.status is OperationStatus.succeeded:
        from app.metrics import META_CAPI_LEAD_EVENTS, META_CAPI_LEAD_LAST_SUCCESS

        delivery.state = "delivered"
        delivery.delivered_at = datetime.now(UTC)
        delivery.next_attempt_at = None
        META_CAPI_LEAD_EVENTS.labels(outcome="succeeded").inc()
        META_CAPI_LEAD_LAST_SUCCESS.set(delivery.delivered_at.timestamp())
    elif result.status in {
        OperationStatus.retryable,
        OperationStatus.reconciliation_required,
    }:
        if delivery.attempt_count >= max_attempts:
            delivery.state = "dead_letter"
            delivery.next_attempt_at = None
            from app.metrics import META_CAPI_LEAD_EVENTS

            META_CAPI_LEAD_EVENTS.labels(outcome="failed").inc()
        else:
            delivery.state = "retryable"
            delay = result.retry_after_seconds or min(
                8 * 60 * 60, 60 * (2 ** max(delivery.attempt_count - 1, 0))
            )
            delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
            from app.metrics import META_CAPI_LEAD_EVENTS

            META_CAPI_LEAD_EVENTS.labels(outcome="retrying").inc()
    else:
        delivery.state = "dead_letter"
        delivery.next_attempt_at = None
        from app.metrics import META_CAPI_LEAD_EVENTS

        META_CAPI_LEAD_EVENTS.labels(outcome="failed").inc()
    db.flush()
    logger.info(
        "Meta CAPI Lead delivery result",
        extra={
            "delivery_id": str(delivery.id),
            "event_id": str((delivery.payload_json or {}).get("event_id") or ""),
            "delivery_state": delivery.state,
            "http_status": delivery.response_status,
            "error_classification": delivery.error_code,
            "meta_trace_id": delivery.external_receipt_json.get("trace_id"),
        },
    )
    return delivery


def deliver_lead(
    db: Session, command: DeliverMetaCapiLeadCommand
) -> IntegrationDelivery:
    def operation() -> IntegrationDelivery:
        if command.context.scope != META_CAPI_DELIVERY_SCOPE:
            raise _error(
                "scope_invalid",
                "Meta CAPI Lead delivery requires its own command scope.",
            )
        return _deliver(db, command.delivery_id)

    return execute_owner_command(
        db, definition=_DELIVER, context=command.context, operation=operation
    )


def due_delivery_ids(db: Session, *, limit: int = 100) -> tuple[UUID, ...]:
    now = datetime.now(UTC)
    rows = db.scalars(
        select(IntegrationDelivery.id)
        .join(IntegrationDelivery.capability_binding)
        .where(
            IntegrationCapabilityBinding.capability_id == META_WEBSITE_LEAD_CAPABILITY,
            (
                (IntegrationDelivery.state == "pending")
                | (
                    (IntegrationDelivery.state == "retryable")
                    & (IntegrationDelivery.next_attempt_at <= now)
                )
                | (
                    (IntegrationDelivery.state == "leased")
                    & (IntegrationDelivery.leased_until <= now)
                )
            ),
        )
        .order_by(IntegrationDelivery.created_at)
        .limit(max(1, min(limit, 500)))
    ).all()
    return tuple(rows)


def health_snapshot(db: Session) -> MetaCapiHealthSnapshot:
    state_rows = db.execute(
        select(IntegrationDelivery.state, func.count(IntegrationDelivery.id))
        .join(IntegrationDelivery.capability_binding)
        .where(
            IntegrationCapabilityBinding.capability_id == META_WEBSITE_LEAD_CAPABILITY
        )
        .group_by(IntegrationDelivery.state)
    ).all()
    rows: dict[str, int] = {str(row[0]): int(row[1]) for row in state_rows}
    deliveries = db.scalars(
        select(IntegrationDelivery)
        .join(IntegrationDelivery.capability_binding)
        .where(
            IntegrationCapabilityBinding.capability_id == META_WEBSITE_LEAD_CAPABILITY
        )
    ).all()
    return MetaCapiHealthSnapshot(
        queued=int(rows.get("pending", 0)),
        succeeded=int(rows.get("delivered", 0)),
        failed=int(rows.get("dead_letter", 0)),
        retrying=int(rows.get("retryable", 0)) + int(rows.get("leased", 0)),
        deduplicated=sum(
            int((item.external_receipt_json or {}).get("deduplicated_count") or 0)
            for item in deliveries
        ),
        last_success_at=max(
            (item.delivered_at for item in deliveries if item.delivered_at is not None),
            default=None,
        ),
    )
