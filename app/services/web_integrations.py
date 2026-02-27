"""Service helpers for admin integrations web routes."""

from __future__ import annotations

import json
import ipaddress
import socket
import urllib.parse
from typing import cast
from uuid import UUID

import httpx

from app.schemas.billing import PaymentProviderCreate
from app.schemas.connector import ConnectorConfigCreate
from app.schemas.integration import IntegrationJobCreate, IntegrationTargetCreate
from app.schemas.webhook import WebhookEndpointCreate, WebhookSubscriptionCreate
from app.services import billing as billing_service
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services import webhook as webhook_service
from app.services.common import validate_enum


def _parse_uuid(value: str | None, field: str, required: bool = True) -> UUID | None:
    if not value:
        if required:
            raise ValueError(f"{field} is required")
        return None
    return UUID(value)


def _parse_json(value: str | None, field: str) -> dict | None:
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def connector_form_options() -> dict[str, object]:
    from app.models.connector import ConnectorAuthType, ConnectorType

    return {
        "connector_types": [t.value for t in ConnectorType],
        "auth_types": [t.value for t in ConnectorAuthType],
    }


def connector_error_state(
    *,
    name: str,
    connector_type: str,
    auth_type: str,
    base_url: str | None,
    timeout_sec: str | None,
    auth_config: str | None,
    headers: str | None,
    retry_policy: str | None,
    metadata: str | None,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **connector_form_options(),
        "form": {
            "name": name,
            "connector_type": connector_type,
            "auth_type": auth_type,
            "base_url": base_url or "",
            "timeout_sec": timeout_sec or "",
            "auth_config": auth_config or "",
            "headers": headers or "",
            "retry_policy": retry_policy or "",
            "metadata": metadata or "",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def target_form_options(db) -> dict[str, object]:
    from app.models.integration import IntegrationTargetType

    connectors = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    return {
        "target_types": [t.value for t in IntegrationTargetType],
        "connectors": connectors,
    }


def target_error_state(
    db,
    *,
    name: str,
    target_type: str,
    connector_config_id: str | None,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **target_form_options(db),
        "form": {
            "name": name,
            "target_type": target_type,
            "connector_config_id": connector_config_id or "",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def job_form_options(db) -> dict[str, object]:
    from app.models.integration import IntegrationJobType, IntegrationScheduleType

    targets = integration_service.integration_targets.list(
        db=db,
        target_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    return {
        "job_types": [t.value for t in IntegrationJobType],
        "schedule_types": [t.value for t in IntegrationScheduleType],
        "targets": targets,
    }


def job_error_state(
    db,
    *,
    target_id: str,
    name: str,
    job_type: str,
    schedule_type: str,
    interval_minutes: str | None,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **job_form_options(db),
        "form": {
            "target_id": target_id,
            "name": name,
            "job_type": job_type,
            "schedule_type": schedule_type,
            "interval_minutes": interval_minutes or "",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def connector_stats(connectors: list) -> dict[str, object]:
    by_type: dict[str, int] = {}
    stats = {
        "total": len(connectors),
        "active": sum(1 for c in connectors if c.is_active),
        "by_type": by_type,
    }
    for connector in connectors:
        ctype = (
            connector.connector_type.value
            if hasattr(connector.connector_type, "value")
            else str(connector.connector_type or "custom")
        )
        by_type[ctype] = by_type.get(ctype, 0) + 1
    return stats


def build_connectors_list_data(db) -> dict[str, object]:
    connectors = connector_service.connector_configs.list_all(
        db=db,
        connector_type=None,
        auth_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    return {
        "connectors": connectors,
        "stats": connector_stats(connectors),
    }


def create_connector(
    db,
    *,
    name: str,
    connector_type: str,
    auth_type: str,
    base_url: str | None,
    timeout_sec: str | None,
    auth_config: str | None,
    headers: str | None,
    retry_policy: str | None,
    metadata: str | None,
    notes: str | None,
    is_active: bool,
):
    from app.models.connector import ConnectorAuthType, ConnectorType

    payload = ConnectorConfigCreate(
        name=name.strip(),
        connector_type=validate_enum(connector_type, ConnectorType, "connector_type"),
        auth_type=validate_enum(auth_type, ConnectorAuthType, "auth_type"),
        base_url=base_url.strip() if base_url else None,
        timeout_sec=int(timeout_sec) if timeout_sec else None,
        auth_config=_parse_json(auth_config, "auth_config"),
        headers=_parse_json(headers, "headers"),
        retry_policy=_parse_json(retry_policy, "retry_policy"),
        metadata_=_parse_json(metadata, "metadata"),
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return connector_service.connector_configs.create(db, payload)


def build_embedded_connector_data(
    db,
    *,
    connector_id: str,
    perform_check: bool = False,
) -> dict[str, object]:
    connector = connector_service.connector_configs.get(db, connector_id)
    base_url = (connector.base_url or "").strip()
    parsed = urllib.parse.urlparse(base_url) if base_url else None
    is_http = bool(parsed and parsed.scheme in {"http", "https"} and parsed.netloc)
    health_status = "ready" if is_http else "misconfigured"
    health_http_status: int | None = None
    probe_checked = False
    if perform_check and is_http:
        probe_checked = True
        health_status, health_http_status, health_message = _probe_embedded_url_health(base_url)
    else:
        health_message = (
            "Connector URL is not configured or invalid. Set a full http(s) base URL to embed this integration."
            if not is_http
            else "Connection appears configured. Use 'Check Connection' to probe reachability."
        )
    return {
        "connector": connector,
        "embed_url": base_url if is_http else "",
        "health_status": health_status,
        "health_http_status": health_http_status,
        "probe_checked": probe_checked,
        "health_message": health_message,
    }


def _probe_embedded_url_health(url: str) -> tuple[str, int | None, str]:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        if hostname:
            addrinfos = socket.getaddrinfo(hostname, None)
            for addrinfo in addrinfos:
                ip = ipaddress.ip_address(addrinfo[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise ValueError("SSRF blocked: target resolves to internal address")
        response = httpx.get(url, timeout=6.0, follow_redirects=True)
    except Exception as exc:
        return (
            "unreachable",
            None,
            f"Connection check failed: {exc}. Confirm DNS/network reachability and remote service availability.",
        )

    status = int(response.status_code)
    if 200 <= status < 300:
        return (
            "ready",
            status,
            f"Connection check succeeded ({status}). If iframe still fails, target may block embedding with X-Frame-Options/CSP.",
        )
    if status in {401, 403}:
        return (
            "auth_required",
            status,
            f"Service is reachable but denied access ({status}). Configure credentials/session and verify embed permissions.",
        )
    if status >= 500:
        return (
            "unreachable",
            status,
            f"Service returned server error ({status}). Check upstream service health and logs.",
        )
    return (
        "degraded",
        status,
        f"Service responded with status {status}. Verify endpoint path and access controls.",
    )


def target_stats(targets: list) -> dict[str, object]:
    by_type: dict[str, int] = {}
    stats = {
        "total": len(targets),
        "active": sum(1 for t in targets if t.is_active),
        "by_type": by_type,
    }
    for target in targets:
        ttype = (
            target.target_type.value
            if hasattr(target.target_type, "value")
            else str(target.target_type or "custom")
        )
        by_type[ttype] = by_type.get(ttype, 0) + 1
    return stats


def build_targets_list_data(db) -> dict[str, object]:
    targets = integration_service.integration_targets.list_all(
        db=db,
        target_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    return {
        "targets": targets,
        "stats": target_stats(targets),
    }


def create_target(
    db,
    *,
    name: str,
    target_type: str,
    connector_config_id: str | None,
    notes: str | None,
    is_active: bool,
):
    from app.models.integration import IntegrationTargetType

    payload = IntegrationTargetCreate(
        name=name.strip(),
        target_type=validate_enum(target_type, IntegrationTargetType, "target_type"),
        connector_config_id=_parse_uuid(
            connector_config_id, "connector_config_id", required=False
        ),
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return integration_service.integration_targets.create(db, payload)


def job_stats(jobs: list) -> dict[str, int]:
    def _schedule_value(item):
        schedule = getattr(item, "schedule_type", None)
        if hasattr(schedule, "value"):
            return schedule.value
        return str(schedule) if schedule else None

    return {
        "total": len(jobs),
        "active": sum(1 for j in jobs if j.is_active),
        "manual": sum(1 for j in jobs if _schedule_value(j) == "manual"),
        "scheduled": sum(1 for j in jobs if _schedule_value(j) == "interval"),
    }


def build_jobs_list_data(db) -> dict[str, object]:
    jobs = integration_service.integration_jobs.list_all(
        db=db,
        target_id=None,
        job_type=None,
        schedule_type=None,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    job_runs = {}
    for job in jobs:
        recent_runs = integration_service.integration_runs.list(
            db=db,
            job_id=str(job.id),
            status=None,
            order_by="started_at",
            order_dir="desc",
            limit=5,
            offset=0,
        )
        job_runs[str(job.id)] = recent_runs
    return {
        "jobs": jobs,
        "job_runs": job_runs,
        "stats": job_stats(jobs),
    }


def create_job(
    db,
    *,
    target_id: str,
    name: str,
    job_type: str,
    schedule_type: str,
    interval_minutes: str | None,
    notes: str | None,
    is_active: bool,
):
    from app.models.integration import IntegrationJobType, IntegrationScheduleType

    interval_value = int(interval_minutes) if interval_minutes else None
    if schedule_type == "interval" and not interval_value:
        raise ValueError("interval_minutes is required for interval schedules")
    payload = IntegrationJobCreate(
        target_id=cast(UUID, _parse_uuid(target_id, "target_id")),
        name=name.strip(),
        job_type=validate_enum(job_type, IntegrationJobType, "job_type"),
        schedule_type=validate_enum(schedule_type, IntegrationScheduleType, "schedule_type"),
        interval_minutes=interval_value,
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return integration_service.integration_jobs.create(db, payload)


def webhook_form_options(db) -> dict[str, object]:
    from app.models.webhook import WebhookEventType

    connectors = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    return {
        "event_types": [t.value for t in WebhookEventType],
        "connectors": connectors,
    }


def webhook_error_state(
    db,
    *,
    name: str,
    url: str,
    connector_config_id: str | None,
    secret: str | None,
    event_types: list[str] | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **webhook_form_options(db),
        "form": {
            "name": name,
            "url": url,
            "connector_config_id": connector_config_id or "",
            "secret": secret or "",
            "event_types": event_types or [],
            "is_active": is_active,
        },
    }


def create_webhook_endpoint(
    db,
    *,
    name: str,
    url: str,
    connector_config_id: str | None,
    secret: str | None,
    event_types: list[str] | None,
    is_active: bool,
):
    from app.models.webhook import WebhookEventType

    payload = WebhookEndpointCreate(
        name=name.strip(),
        url=url.strip(),
        connector_config_id=_parse_uuid(
            connector_config_id, "connector_config_id", required=False
        ),
        secret=secret.strip() if secret else None,
        is_active=is_active,
    )
    endpoint = webhook_service.webhook_endpoints.create(db, payload)
    for event_type in event_types or []:
        subscription_payload = WebhookSubscriptionCreate(
            endpoint_id=endpoint.id,
            event_type=validate_enum(event_type, WebhookEventType, "event_type"),
            is_active=True,
        )
        webhook_service.webhook_subscriptions.create(db, subscription_payload)
    return endpoint


def build_webhooks_list_data(db) -> dict[str, object]:
    endpoints = webhook_service.webhook_endpoints.list_all(
        db=db,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )

    endpoint_stats = {}
    for endpoint in endpoints:
        subs = webhook_service.webhook_subscriptions.list_all(
            db=db,
            endpoint_id=str(endpoint.id),
            event_type=None,
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        pending = webhook_service.webhook_deliveries.list(
            db=db,
            endpoint_id=str(endpoint.id),
            subscription_id=None,
            event_type=None,
            status="pending",
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        failed = webhook_service.webhook_deliveries.list(
            db=db,
            endpoint_id=str(endpoint.id),
            subscription_id=None,
            event_type=None,
            status="failed",
            order_by="created_at",
            order_dir="desc",
            limit=1000,
            offset=0,
        )
        endpoint_stats[str(endpoint.id)] = {
            "subscriptions": len(subs),
            "pending": len(pending),
            "failed": len(failed),
        }
    stats = {
        "total": len(endpoints),
        "active": sum(1 for e in endpoints if e.is_active),
    }
    return {
        "endpoints": endpoints,
        "endpoint_stats": endpoint_stats,
        "stats": stats,
    }


def provider_form_options(db) -> dict[str, object]:
    from app.models.billing import PaymentProviderType

    connectors = connector_service.connector_configs.list(
        db=db,
        connector_type=None,
        auth_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    return {
        "provider_types": [t.value for t in PaymentProviderType],
        "connectors": connectors,
    }


def provider_error_state(
    db,
    *,
    name: str,
    provider_type: str,
    connector_config_id: str | None,
    webhook_secret_ref: str | None,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **provider_form_options(db),
        "form": {
            "name": name,
            "provider_type": provider_type,
            "connector_config_id": connector_config_id or "",
            "webhook_secret_ref": webhook_secret_ref or "",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def create_provider(
    db,
    *,
    name: str,
    provider_type: str,
    connector_config_id: str | None,
    webhook_secret_ref: str | None,
    notes: str | None,
    is_active: bool,
):
    from app.models.billing import PaymentProviderType

    payload = PaymentProviderCreate(
        name=name.strip(),
        provider_type=validate_enum(provider_type, PaymentProviderType, "provider_type"),
        connector_config_id=_parse_uuid(
            connector_config_id, "connector_config_id", required=False
        ),
        webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return billing_service.payment_providers.create(db, payload)


def build_providers_list_data(db) -> dict[str, object]:
    providers = billing_service.payment_providers.list_all(
        db=db,
        order_by="name",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    by_type: dict[str, int] = {}
    stats = {
        "total": len(providers),
        "active": sum(1 for p in providers if p.is_active),
        "by_type": by_type,
    }
    for provider in providers:
        ptype = (
            provider.provider_type.value
            if hasattr(provider.provider_type, "value")
            else str(provider.provider_type or "manual")
        )
        by_type[ptype] = by_type.get(ptype, 0) + 1
    return {
        "providers": providers,
        "stats": stats,
    }


def build_webhook_detail_data(db, *, endpoint_id: str) -> dict[str, object]:
    endpoint = webhook_service.webhook_endpoints.get(db, endpoint_id)
    subscriptions = webhook_service.webhook_subscriptions.list_all(
        db=db,
        endpoint_id=str(endpoint.id),
        event_type=None,
        order_by="created_at",
        order_dir="desc",
        limit=1000,
        offset=0,
    )
    deliveries = webhook_service.webhook_deliveries.list(
        db=db,
        endpoint_id=str(endpoint.id),
        subscription_id=None,
        event_type=None,
        status=None,
        order_by="created_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    return {
        "endpoint": endpoint,
        "subscriptions": subscriptions,
        "deliveries": deliveries,
    }


def build_provider_detail_data(db, *, provider_id: str) -> dict[str, object]:
    provider = billing_service.payment_providers.get(db, provider_id)
    events = billing_service.payment_provider_events.list(
        db=db,
        provider_id=str(provider.id),
        payment_id=None,
        invoice_id=None,
        status=None,
        order_by="received_at",
        order_dir="desc",
        limit=50,
        offset=0,
    )
    return {"provider": provider, "events": events}
