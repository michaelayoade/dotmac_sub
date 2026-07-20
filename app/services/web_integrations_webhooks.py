"""Admin adapter for capability-bound outbound HTTP event delivery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationEventSubscription,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.services.events.types import EventType
from app.services.integrations import delivery, installations
from app.services.integrations.connectors.http_webhook import EVENT_DELIVERY_CAPABILITY
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)

CONNECTOR_KEY = "webhook.http"


def _optional_int(value: str | int | None, *, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _event_values(event_types: list[str] | None) -> tuple[str, ...]:
    allowed = {item.value for item in EventType}
    normalized = tuple(dict.fromkeys(str(item).strip() for item in event_types or ()))
    invalid = sorted(item for item in normalized if item not in allowed)
    if invalid:
        raise ValueError("Unsupported event type(s): " + ", ".join(invalid))
    if not normalized:
        raise ValueError("Select at least one event type")
    return normalized


def _binding(installation: IntegrationInstallation) -> IntegrationCapabilityBinding:
    for binding in installation.capability_bindings:
        if binding.capability_id == EVENT_DELIVERY_CAPABILITY:
            return binding
    raise ValueError("events.deliver.v1 binding is missing")


def _require_endpoint(db: Session, endpoint_id: str) -> IntegrationInstallation:
    installation = installations.get_installation(db, endpoint_id)
    if installation.connector_key != CONNECTOR_KEY:
        raise ValueError("HTTP webhook installation not found")
    return installation


def webhook_form_options(_db: Session) -> dict[str, object]:
    return {"event_types": [item.value for item in EventType]}


def webhook_error_state(
    db: Session,
    *,
    name: str,
    url: str,
    signing_secret_ref: str | None,
    authorization_ref: str | None,
    event_types: list[str] | None,
    is_active: bool,
    delivery_timeout_seconds: str | int | None = None,
    max_retries: str | int | None = None,
) -> dict[str, object]:
    return {
        **webhook_form_options(db),
        "form": {
            "name": name,
            "url": url,
            "signing_secret_ref": "" if signing_secret_ref else "",
            "authorization_ref": "" if authorization_ref else "",
            "event_types": event_types or [],
            "is_active": is_active,
            "delivery_timeout_seconds": delivery_timeout_seconds or "",
            "max_retries": max_retries or "",
        },
    }


def create_webhook_endpoint(
    db: Session,
    *,
    name: str,
    url: str,
    signing_secret_ref: str | None,
    authorization_ref: str | None,
    event_types: list[str] | None,
    is_active: bool,
    delivery_timeout_seconds: str | int | None = None,
    max_retries: str | int | None = None,
) -> IntegrationInstallation:
    if is_active:
        raise ValueError("Save the endpoint, then validate its connection to enable it")
    actor = "admin.integrations.webhooks"
    endpoint = installations.create_draft(
        db,
        connector_key=CONNECTOR_KEY,
        name=name,
        actor=actor,
    )
    secret_refs = {}
    if signing_secret_ref and signing_secret_ref.strip():
        secret_refs["signing_secret"] = signing_secret_ref.strip()
    if authorization_ref and authorization_ref.strip():
        secret_refs["authorization"] = authorization_ref.strip()
    installations.create_config_revision(
        db,
        installation_id=endpoint.id,
        config={
            "url": url.strip(),
            "method": "POST",
            "timeout_seconds": _optional_int(delivery_timeout_seconds, default=30),
            "max_attempts": _optional_int(max_retries, default=10),
        },
        secret_refs=secret_refs,
        actor=actor,
    )
    host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    binding = installations.bind_capability(
        db,
        installation_id=endpoint.id,
        capability_id=EVENT_DELIVERY_CAPABILITY,
        policy={"approved_egress_hosts": [host]},
        actor=actor,
    )
    result = installations.validate_static(db, installation_id=endpoint.id, actor=actor)
    if not result.valid:
        raise ValueError(
            "Webhook configuration is invalid: " + ", ".join(result.error_codes)
        )
    delivery.sync_event_subscriptions(
        db,
        binding=binding,
        event_types=_event_values(event_types),
        actor=actor,
    )
    return installations.commit_installation_changes(db, endpoint)


def update_webhook_endpoint(
    db: Session,
    *,
    endpoint_id: str,
    name: str,
    url: str,
    signing_secret_ref: str | None,
    authorization_ref: str | None,
    event_types: list[str] | None,
    is_active: bool,
    delivery_timeout_seconds: str | int | None = None,
    max_retries: str | int | None = None,
) -> IntegrationInstallation:
    endpoint = _require_endpoint(db, endpoint_id)
    actor = "admin.integrations.webhooks"
    current = endpoint.current_config_revision
    secret_refs = dict(current.secret_refs if current is not None else {})
    if signing_secret_ref and signing_secret_ref.strip():
        secret_refs["signing_secret"] = signing_secret_ref.strip()
    if authorization_ref and authorization_ref.strip():
        secret_refs["authorization"] = authorization_ref.strip()
    endpoint.name = name.strip()
    installations.create_config_revision(
        db,
        installation_id=endpoint.id,
        config={
            "url": url.strip(),
            "method": "POST",
            "timeout_seconds": _optional_int(delivery_timeout_seconds, default=30),
            "max_attempts": _optional_int(max_retries, default=10),
        },
        secret_refs=secret_refs,
        actor=actor,
    )
    host = (urlparse(url.strip()).hostname or "").lower().rstrip(".")
    binding = installations.bind_capability(
        db,
        installation_id=endpoint.id,
        capability_id=EVENT_DELIVERY_CAPABILITY,
        policy={"approved_egress_hosts": [host]},
        actor=actor,
    )
    result = installations.validate_static(db, installation_id=endpoint.id, actor=actor)
    if not result.valid:
        raise ValueError(
            "Webhook configuration is invalid: " + ", ".join(result.error_codes)
        )
    delivery.sync_event_subscriptions(
        db,
        binding=binding,
        event_types=_event_values(event_types),
        actor=actor,
    )
    if is_active:
        raise ValueError(
            "Configuration changed; validate the connection before enabling"
        )
    return installations.commit_installation_changes(db, endpoint)


def set_webhook_endpoint_active(
    db: Session, *, endpoint_id: str, is_active: bool
) -> IntegrationInstallation:
    endpoint = _require_endpoint(db, endpoint_id)
    actor = "admin.integrations.webhooks"
    if not is_active:
        installations.disable_installation(
            db,
            installation_id=endpoint.id,
            reason="operator_disabled",
            actor=actor,
        )
    else:
        static_result = installations.validate_static(
            db, installation_id=endpoint.id, actor=actor
        )
        if not static_result.valid:
            raise ValueError(
                "Static validation failed: " + ", ".join(static_result.error_codes)
            )
        context = build_execution_context(
            db,
            capability_binding_id=_binding(endpoint).id,
            allow_disabled=True,
        )
        connection_result = validate_connection(context)
        installations.enable_after_connection_validation(
            db,
            installation_id=endpoint.id,
            connection_result=connection_result,
            actor=actor,
        )
    return installations.commit_installation_changes(db, endpoint)


def delete_webhook_endpoint(db: Session, *, endpoint_id: str) -> None:
    endpoint = _require_endpoint(db, endpoint_id)
    installations.retire_installation(
        db,
        installation_id=endpoint.id,
        reason="operator_retired",
        actor="admin.integrations.webhooks",
    )
    installations.commit_installation_changes(db, endpoint)


def queue_webhook_test_delivery(
    db: Session, *, endpoint_id: str
) -> IntegrationDelivery:
    endpoint = _require_endpoint(db, endpoint_id)
    if endpoint.state != IntegrationInstallationState.enabled.value:
        raise ValueError("Validate and enable the endpoint before sending a test")
    binding = _binding(endpoint)
    return delivery.create_manual_test_delivery(
        db,
        capability_binding_id=binding.id,
    )


def _endpoint_view(endpoint: IntegrationInstallation) -> SimpleNamespace:
    revision = endpoint.current_config_revision
    config = dict(revision.config_json if revision is not None else {})
    refs = dict(revision.secret_refs if revision is not None else {})
    return SimpleNamespace(
        id=endpoint.id,
        name=endpoint.name,
        url=config.get("url", ""),
        is_active=endpoint.state == IntegrationInstallationState.enabled.value,
        state=endpoint.state,
        secret=bool(refs.get("signing_secret")),
        signing_secret_ref_configured=bool(refs.get("signing_secret")),
        authorization_ref_configured=bool(refs.get("authorization")),
        delivery_timeout_seconds=config.get("timeout_seconds", 30),
        max_retries=config.get("max_attempts", 10),
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def build_webhooks_list_data(db: Session) -> dict[str, object]:
    installations_list = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.connector_key == CONNECTOR_KEY,
            IntegrationInstallation.state != IntegrationInstallationState.retired.value,
        )
        .order_by(IntegrationInstallation.name.asc())
        .all()
    )
    endpoints = [_endpoint_view(item) for item in installations_list]
    endpoint_stats: dict[str, dict[str, int]] = {}
    for endpoint in installations_list:
        binding = _binding(endpoint)
        subscriptions = db.query(IntegrationEventSubscription).filter(
            IntegrationEventSubscription.capability_binding_id == binding.id,
            IntegrationEventSubscription.state == "enabled",
        )
        deliveries = db.query(IntegrationDelivery).filter(
            IntegrationDelivery.capability_binding_id == binding.id
        )
        endpoint_stats[str(endpoint.id)] = {
            "subscriptions": subscriptions.count(),
            "pending": deliveries.filter(
                IntegrationDelivery.state.in_(("pending", "leased", "retryable"))
            ).count(),
            "failed": deliveries.filter(
                IntegrationDelivery.state.in_(
                    ("dead_letter", "reconciliation_required")
                )
            ).count(),
        }
    return {
        "endpoints": endpoints,
        "endpoint_stats": endpoint_stats,
        "stats": {
            "total": len(endpoints),
            "active": sum(1 for endpoint in endpoints if endpoint.is_active),
        },
    }


def build_webhook_detail_data(db: Session, *, endpoint_id: str) -> dict[str, object]:
    endpoint = _require_endpoint(db, endpoint_id)
    binding = _binding(endpoint)
    subscriptions = (
        db.query(IntegrationEventSubscription)
        .filter(
            IntegrationEventSubscription.capability_binding_id == binding.id,
            IntegrationEventSubscription.state == "enabled",
        )
        .order_by(IntegrationEventSubscription.event_type.asc())
        .all()
    )
    deliveries = (
        db.query(IntegrationDelivery)
        .filter(IntegrationDelivery.capability_binding_id == binding.id)
        .order_by(IntegrationDelivery.created_at.desc())
        .limit(50)
        .all()
    )
    failures = [
        item
        for item in deliveries
        if item.state in {"dead_letter", "reconciliation_required"}
    ]
    return {
        "endpoint": _endpoint_view(endpoint),
        "subscriptions": subscriptions,
        "deliveries": deliveries,
        "delivery_summary": SimpleNamespace(
            latest_delivery=deliveries[0] if deliveries else None,
            latest_failure=failures[0] if failures else None,
            pending_count=sum(
                item.state in {"pending", "leased", "retryable"} for item in deliveries
            ),
            failed_count=len(failures),
            delivered_count=sum(item.state == "delivered" for item in deliveries),
        ),
    }


def build_webhook_edit_data(db: Session, *, endpoint_id: str) -> dict[str, object]:
    endpoint = _require_endpoint(db, endpoint_id)
    detail = build_webhook_detail_data(db, endpoint_id=endpoint_id)
    view = cast(SimpleNamespace, detail["endpoint"])
    subscriptions = cast(list[IntegrationEventSubscription], detail["subscriptions"])
    return {
        **webhook_form_options(db),
        "endpoint": view,
        "form": {
            "name": view.name,
            "url": view.url,
            "signing_secret_ref": "",
            "authorization_ref": "",
            "signing_secret_ref_configured": view.signing_secret_ref_configured,
            "authorization_ref_configured": view.authorization_ref_configured,
            "event_types": [item.event_type for item in subscriptions],
            "is_active": view.is_active,
            "delivery_timeout_seconds": view.delivery_timeout_seconds,
            "max_retries": view.max_retries,
        },
        "action_url": f"/admin/integrations/webhooks/{view.id}",
        "submit_label": "Save Webhook",
    }
