"""Service helpers for admin integrations web routes."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.connector import ConnectorAuthType, ConnectorConfig, ConnectorType
from app.models.integration import (
    IntegrationJob,
    IntegrationRun,
    IntegrationRunStatus,
)
from app.models.integration_platform import (
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.schemas.connector import ConnectorConfigCreate, ConnectorConfigUpdate
from app.schemas.integration import IntegrationJobCreate, IntegrationTargetCreate
from app.services import connector as connector_service
from app.services import integration as integration_service
from app.services.common import validate_enum
from app.services.integrations import installations
from app.services.integrations import registry as integration_registry
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)
from app.services.list_query import (
    ListDefinition,
    ListFieldDefinition,
    ListQuery,
    PageMeta,
)
from app.validators.forms import parse_uuid

logger = logging.getLogger(__name__)

INSTALLED_LIST_DEFINITION = ListDefinition(
    key="installed-integrations",
    fields=(
        ListFieldDefinition("name", "Integration", searchable=True, sortable=True),
        ListFieldDefinition("status", "State", filterable=True),
    ),
    default_sort="name",
    default_sort_dir="asc",
)


def installed_list_query(
    *,
    search: str = "",
    status: str = "all",
    sort: str = "name",
    direction: str = "asc",
    page: int = 1,
    per_page: int = 25,
) -> ListQuery:
    return INSTALLED_LIST_DEFINITION.build_query(
        search=search[:160],
        filters={
            "status": status
            if status
            in {"all", "draft", "validating", "disabled", "enabled", "quarantined"}
            else "all"
        },
        sort_by=sort if sort in INSTALLED_LIST_DEFINITION.sortable_keys else "name",
        sort_dir=direction if direction in {"asc", "desc"} else "asc",
        page=max(1, page),
        per_page=per_page
        if per_page in INSTALLED_LIST_DEFINITION.per_page_options
        else 25,
    )


@dataclass(frozen=True, slots=True)
class IntegrationOperationalEvidence:
    """Bounded runtime facts for one installed integration."""

    last_result: str
    observed_at: datetime | None
    calls: int
    failed: int
    last_run_status: str | None
    needs_attention: bool

    def to_template_data(self) -> dict[str, object]:
        """Serialize at the web-template adapter boundary."""
        return {
            "last_result": self.last_result,
            "observed_at": self.observed_at,
            "calls": self.calls,
            "failed": self.failed,
            "last_run_status": self.last_run_status,
            "needs_attention": self.needs_attention,
        }


@dataclass(frozen=True, slots=True)
class InstalledIntegrationProjection:
    """Typed installation state plus separately sourced runtime evidence."""

    installation: IntegrationInstallation
    title: str
    root: str
    integration_type: str
    operational_evidence: IntegrationOperationalEvidence
    manage_url: str

    def to_template_data(self) -> dict[str, object]:
        """Serialize at the web-template adapter boundary."""
        return {
            "installation": self.installation,
            "title": self.title,
            "root": self.root,
            "integration_type": self.integration_type,
            "operational_evidence": self.operational_evidence.to_template_data(),
            "manage_url": self.manage_url,
        }


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
    return {
        "connector_types": [t.value for t in ConnectorType],
        "auth_types": [t.value for t in ConnectorAuthType],
    }


@dataclass(frozen=True, slots=True, repr=False)
class ConnectorFormCommand:
    auth_type: ConnectorAuthType
    base_url: str | None
    timeout_sec: int | None
    username: str | None
    password: str | None
    token: str | None
    api_key: str | None
    secret: str | None
    client_id: str | None
    client_secret: str | None
    header_names: tuple[str, ...]
    header_values: tuple[str, ...]
    retry_max_attempts: int | None
    retry_backoff_sec: int | None
    notes: str | None
    is_active: bool
    name: str | None = None
    connector_type: ConnectorType | None = None
    clear_base_url: bool = False


def _auth_fields(command: ConnectorFormCommand) -> dict[str, str]:
    fields = {
        ConnectorAuthType.basic: (
            ("username", command.username),
            ("password", command.password),
        ),
        ConnectorAuthType.bearer: (("token", command.token),),
        ConnectorAuthType.api_key: (("api_key", command.api_key),),
        ConnectorAuthType.hmac: (("secret", command.secret),),
        ConnectorAuthType.oauth2: (
            ("client_id", command.client_id),
            ("client_secret", command.client_secret),
        ),
    }.get(command.auth_type, ())
    return {key: value for key, value in fields if value and value.strip()}


def _validate_auth_fields(auth_type: ConnectorAuthType, auth: dict[str, str]) -> None:
    required = {
        ConnectorAuthType.basic: ("username", "password"),
        ConnectorAuthType.bearer: ("token",),
        ConnectorAuthType.api_key: ("api_key",),
        ConnectorAuthType.hmac: ("secret",),
        ConnectorAuthType.oauth2: ("client_id", "client_secret"),
    }.get(auth_type, ())
    missing = [key.replace("_", " ") for key in required if not auth.get(key)]
    if missing:
        raise ValueError(
            f"Enter {', '.join(missing)} for {auth_type.value} authentication."
        )


def _header_fields(command: ConnectorFormCommand) -> dict[str, str]:
    if len(command.header_names) != len(command.header_values):
        raise ValueError("Each header needs a name and a value.")
    headers: dict[str, str] = {}
    for raw_name, value in zip(
        command.header_names, command.header_values, strict=True
    ):
        name = raw_name.strip()
        if not name and not value:
            continue
        if (
            not name
            or not name.isascii()
            or any(not (char.isalnum() or char in "!#$%&'*+-.^_`|~") for char in name)
        ):
            raise ValueError("Header names may contain only HTTP header characters.")
        if "\r" in value or "\n" in value:
            raise ValueError("Header values cannot contain line breaks.")
        if name.lower() in (key.lower() for key in headers):
            raise ValueError(f"Header {name} is listed more than once.")
        headers[name] = value
    return headers


def _http_base_url(value: str | None) -> str | None:
    url = (value or "").strip()
    if not url:
        return None
    if len(url) > 500:
        raise ValueError("Base URL must be 500 characters or fewer.")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("Base URL cannot contain control characters.")
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid HTTP(S) base URL.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Enter an HTTP(S) base URL without credentials or query parameters. "
            "Use the integration's secret setup for credentials."
        )
    return url


def _retry_fields(command: ConnectorFormCommand) -> dict[str, int]:
    attempts = command.retry_max_attempts
    backoff = command.retry_backoff_sec
    if attempts is not None and not 1 <= attempts <= 10:
        raise ValueError("Retry attempts must be between 1 and 10.")
    if backoff is not None and not 0 <= backoff <= 3600:
        raise ValueError("Retry delay must be between 0 and 3600 seconds.")
    return {
        key: value
        for key, value in (("max_attempts", attempts), ("backoff_sec", backoff))
        if value is not None
    }


def connector_error_state(
    command: ConnectorFormCommand,
) -> dict[str, object]:
    try:
        safe_base_url = _http_base_url(command.base_url) or ""
    except ValueError:
        safe_base_url = ""
    header_rows = [
        {
            "name": name,
            "value": value if value == SECRET_VALUE_SENTINEL else "",
            "secret": value == SECRET_VALUE_SENTINEL,
        }
        for name, value in zip(command.header_names, command.header_values)
    ]
    return {
        **connector_form_options(),
        "form": {
            "name": command.name or "",
            "connector_type": command.connector_type.value
            if command.connector_type
            else "custom",
            "auth_type": command.auth_type.value,
            "base_url": safe_base_url,
            "timeout_sec": command.timeout_sec or "",
            "username": command.username or "",
            "client_id": command.client_id or "",
            "header_rows": header_rows or [{"name": "", "value": "", "secret": False}],
            "retry_max_attempts": command.retry_max_attempts or "",
            "retry_backoff_sec": command.retry_backoff_sec
            if command.retry_backoff_sec is not None
            else "",
            "notes": command.notes or "",
            "is_active": command.is_active,
        },
    }


def connector_edit_form(connector: ConnectorConfig) -> dict[str, object]:
    auth = getattr(connector, "auth_config", None) or {}
    headers = getattr(connector, "headers", None) or {}
    retry = getattr(connector, "retry_policy", None) or {}
    return {
        "auth_type": connector.auth_type.value,
        "base_url": "",
        "timeout_sec": connector.timeout_sec or "",
        "username": auth.get("username", ""),
        "client_id": auth.get("client_id", ""),
        "header_rows": [
            {
                "name": name,
                "value": SECRET_VALUE_SENTINEL if value else "",
                "secret": bool(value),
            }
            for name, value in headers.items()
        ]
        or [{"name": "", "value": "", "secret": False}],
        "retry_max_attempts": retry.get("max_attempts", ""),
        "retry_backoff_sec": retry.get("backoff_sec", ""),
        "notes": connector.notes or "",
        "is_active": connector.is_active,
    }


def target_form_options(db) -> dict[str, object]:
    from app.models.integration import IntegrationTargetType

    return {"target_types": [t.value for t in IntegrationTargetType]}


def target_error_state(
    db,
    *,
    name: str,
    target_type: str,
    notes: str | None,
    is_active: bool,
) -> dict[str, object]:
    return {
        **target_form_options(db),
        "form": {
            "name": name,
            "target_type": target_type,
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def job_form_options(db) -> dict[str, object]:
    from app.models.integration import IntegrationJobType, IntegrationScheduleType
    from app.models.integration_platform import (
        IntegrationBindingState,
        IntegrationCapabilityBinding,
        IntegrationInstallation,
        IntegrationInstallationState,
    )

    targets = integration_service.integration_targets.list(
        db=db,
        target_type=None,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=1000,
        offset=0,
    )
    bindings = (
        db.query(IntegrationCapabilityBinding)
        .join(IntegrationInstallation)
        .filter(
            IntegrationCapabilityBinding.state == IntegrationBindingState.enabled.value,
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value,
        )
        .order_by(
            IntegrationInstallation.name.asc(),
            IntegrationCapabilityBinding.capability_id.asc(),
        )
        .all()
    )
    return {
        "job_types": [t.value for t in IntegrationJobType],
        "schedule_types": [t.value for t in IntegrationScheduleType],
        "directions": ["pull", "push", "bidirectional"],
        "trigger_modes": ["manual", "schedule", "event", "webhook"],
        "conflict_policies": [
            "remote_wins",
            "local_wins",
            "newest_wins",
            "manual_review",
        ],
        "targets": targets,
        "capability_bindings": bindings,
    }


def job_error_state(
    db,
    *,
    target_id: str,
    name: str,
    job_type: str,
    schedule_type: str,
    interval_minutes: str | None,
    capability_binding_id: str | None = None,
    entity_type: str | None = None,
    direction: str | None = None,
    trigger_mode: str | None = None,
    mapping_config: str | None = None,
    filter_config: str | None = None,
    conflict_policy: str | None = None,
    notes: str | None = None,
    is_active: bool = True,
) -> dict[str, object]:
    return {
        **job_form_options(db),
        "form": {
            "target_id": target_id,
            "name": name,
            "job_type": job_type,
            "schedule_type": schedule_type,
            "interval_minutes": interval_minutes or "",
            "capability_binding_id": capability_binding_id or "",
            "entity_type": entity_type or "",
            "direction": direction or "pull",
            "trigger_mode": trigger_mode or "manual",
            "mapping_config": mapping_config or "",
            "filter_config": filter_config or "",
            "conflict_policy": conflict_policy or "remote_wins",
            "notes": notes or "",
            "is_active": is_active,
        },
    }


def build_marketplace_data(db) -> dict[str, object]:
    installed_rows = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.state != IntegrationInstallationState.retired.value
        )
        .order_by(IntegrationInstallation.created_at.asc())
        .all()
    )
    installed_by_key: dict[str, list[IntegrationInstallation]] = {}
    for installation in installed_rows:
        installed_by_key.setdefault(installation.connector_key, []).append(installation)
    discovered = integration_registry.discover_connectors()
    install_urls = {
        "whatsapp": "/admin/integrations/whatsapp/config",
        "meta.social": "/admin/crm/meta",
        "paystack": "/admin/integrations/payment-gateways/paystack",
        "flutterwave": "/admin/integrations/payment-gateways/flutterwave",
    }
    cards: list[dict[str, object]] = []
    for entry in discovered:
        matches = installed_by_key.get(entry.key, [])
        installed = matches[0] if len(matches) == 1 else None
        cards.append(
            {
                "key": entry.key,
                "name": entry.name,
                "description": entry.description,
                "type": entry.connector_type,
                "installed_count": len(matches),
                "manage_url": _installation_manage_url(installed)
                if installed
                else None,
                "install_url": install_urls.get(entry.key),
            }
        )

    return {"marketplace_cards": cards}


def _installation_manage_url(installation: IntegrationInstallation) -> str:
    if installation.connector_key == "dotmac.erp":
        return "/admin/integrations/erp"
    if installation.connector_key == "webhook.http":
        return f"/admin/integrations/webhooks/{installation.id}"
    if installation.connector_key == "whatsapp":
        return "/admin/integrations/whatsapp/config"
    if installation.connector_key == "nextcloud.talk":
        return f"/admin/integrations/nextcloud-talk/{installation.id}"
    if installation.connector_key == "meta.social":
        return "/admin/crm/meta"
    if installation.connector_key in {"paystack", "flutterwave"}:
        return f"/admin/integrations/payment-gateways/{installation.connector_key}"
    return "/admin/integrations/jobs"


def _latest_run_by_installation(
    db: Session, installation_ids: list[UUID]
) -> dict[str, tuple[str, datetime | None]]:
    """Most recent completed capability run per installation."""
    if not installation_ids:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=IntegrationRun.installation_id,
            order_by=IntegrationRun.started_at.desc(),
        )
        .label("rank")
    )
    ranked = (
        db.query(
            IntegrationRun.installation_id.label("installation_id"),
            IntegrationRun.status.label("status"),
            IntegrationRun.started_at.label("started_at"),
            rank,
        )
        .filter(IntegrationRun.installation_id.in_(installation_ids))
        .filter(IntegrationRun.status != IntegrationRunStatus.running)
        .subquery()
    )
    rows = (
        db.query(ranked.c.installation_id, ranked.c.status, ranked.c.started_at)
        .filter(ranked.c.rank == 1)
        .all()
    )
    return {
        str(row.installation_id): (
            getattr(row.status, "value", str(row.status)),
            row.started_at,
        )
        for row in rows
    }


def _delivery_stats_by_installation(
    db: Session, installation_ids: list[UUID]
) -> dict[str, tuple[int, int]]:
    if not installation_ids:
        return {}
    rows = (
        db.query(
            IntegrationCapabilityBinding.installation_id,
            func.count(IntegrationDelivery.id).label("calls"),
            func.sum(
                case(
                    (
                        IntegrationDelivery.state.in_(
                            ("dead_letter", "reconciliation_required")
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("failed"),
        )
        .join(
            IntegrationDelivery,
            IntegrationDelivery.capability_binding_id
            == IntegrationCapabilityBinding.id,
        )
        .filter(IntegrationCapabilityBinding.installation_id.in_(installation_ids))
        .group_by(IntegrationCapabilityBinding.installation_id)
        .all()
    )
    return {
        str(row.installation_id): (int(row.calls or 0), int(row.failed or 0))
        for row in rows
    }


def _installation_evidence(
    last_run: tuple[str, datetime | None] | None,
    delivery_stats: tuple[int, int] | None,
) -> IntegrationOperationalEvidence:
    """Explain runtime evidence without collapsing it into a health label."""
    calls, failed = delivery_stats or (0, 0)
    last_status = last_run[0] if last_run else None
    if last_status == "success":
        result = "The last completed integration job succeeded."
    elif last_status == "failed":
        result = "The last completed integration job failed."
    elif calls:
        result = f"{calls} delivery attempt(s) recorded; {failed} require attention."
    else:
        result = "No completed job or delivery evidence has been recorded."
    return IntegrationOperationalEvidence(
        last_result=result,
        observed_at=last_run[1] if last_run else None,
        calls=calls,
        failed=failed,
        last_run_status=last_status,
        needs_attention=bool(last_status == "failed" or failed),
    )


def installed_integration_projections(
    db: Session,
) -> tuple[InstalledIntegrationProjection, ...]:
    """Return the typed installation/evidence projection for UI consumers."""
    installed = (
        db.query(IntegrationInstallation)
        .filter(
            IntegrationInstallation.state != IntegrationInstallationState.retired.value
        )
        .order_by(
            IntegrationInstallation.connector_key.asc(),
            IntegrationInstallation.name.asc(),
        )
        .all()
    )
    installation_ids = [installation.id for installation in installed]
    last_runs = _latest_run_by_installation(db, installation_ids)
    delivery_stats = _delivery_stats_by_installation(db, installation_ids)
    projections: list[InstalledIntegrationProjection] = []
    for installation in installed:
        definition = integration_registry.require_connector_definition(
            installation.connector_key
        )
        projections.append(
            InstalledIntegrationProjection(
                installation=installation,
                title=definition.name,
                root="integrations",
                integration_type=definition.connector_type,
                operational_evidence=_installation_evidence(
                    last_runs.get(str(installation.id)),
                    delivery_stats.get(str(installation.id)),
                ),
                manage_url=_installation_manage_url(installation),
            )
        )
    return tuple(projections)


def build_installed_integrations_data(
    db: Session,
    *,
    activity_installation_id: UUID | None = None,
    list_query: ListQuery | None = None,
) -> dict[str, object]:
    all_projections = installed_integration_projections(db)
    projections = all_projections
    installed = [projection.installation for projection in all_projections]
    installation_ids = [installation.id for installation in installed]
    list_query = list_query or installed_list_query()
    if list_query.search:
        needle = list_query.search.casefold()
        projections = tuple(
            row for row in projections if needle in row.installation.name.casefold()
        )
    status = list_query.filter_value("status")
    if status and status != "all":
        projections = tuple(
            row for row in projections if row.installation.state == status
        )
    projections = tuple(
        sorted(
            projections,
            key=lambda row: (
                row.installation.name.casefold(),
                str(row.installation.id),
            ),
            reverse=list_query.sort_dir == "desc",
        )
    )
    page_meta = PageMeta.from_query(list_query, len(projections))
    if page_meta.page != list_query.page:
        list_query = list_query.with_page(page_meta.page)
        page_meta = PageMeta.from_query(list_query, len(projections))
    rows = [
        row.to_template_data()
        for row in projections[
            list_query.offset : list_query.offset + list_query.per_page
        ]
    ]
    selected = (
        next(
            (
                row.to_template_data()
                for row in all_projections
                if row.installation.id == activity_installation_id
            ),
            None,
        )
        if activity_installation_id is not None
        else None
    )

    activities: list[dict[str, object]] = []
    if activity_installation_id in installation_ids:
        run_rows = (
            db.query(
                IntegrationRun,
                IntegrationJob.name.label("job_name"),
            )
            .join(IntegrationJob, IntegrationRun.job_id == IntegrationJob.id)
            .filter(IntegrationRun.installation_id == activity_installation_id)
            .order_by(IntegrationRun.started_at.desc())
            .limit(50)
            .all()
        )
        for run, job_name in run_rows:
            duration_ms = None
            if run.finished_at is not None and run.started_at is not None:
                duration_ms = int(
                    (run.finished_at - run.started_at).total_seconds() * 1000
                )
            activities.append(
                {
                    "installation_id": str(run.installation_id),
                    "timestamp": run.started_at,
                    "event_type": f"job: {job_name}",
                    "status_code": None,
                    "response_time_ms": duration_ms,
                    "status": getattr(run.status, "value", str(run.status)),
                }
            )
        delivery_rows = (
            db.query(IntegrationDelivery, IntegrationCapabilityBinding.installation_id)
            .join(
                IntegrationCapabilityBinding,
                IntegrationDelivery.capability_binding_id
                == IntegrationCapabilityBinding.id,
            )
            .filter(
                IntegrationCapabilityBinding.installation_id == activity_installation_id
            )
            .order_by(IntegrationDelivery.created_at.desc())
            .limit(50)
            .all()
        )
        for delivery_row, delivery_installation_id in delivery_rows:
            activities.append(
                {
                    "installation_id": str(delivery_installation_id),
                    "timestamp": delivery_row.last_attempt_at
                    or delivery_row.created_at,
                    "event_type": f"event: {delivery_row.event_type}",
                    "status_code": delivery_row.response_status,
                    "response_time_ms": None,
                    "status": delivery_row.state,
                }
            )
    activities.sort(key=lambda item: item["timestamp"], reverse=True)
    activities = activities[:50]
    return {
        "integrations": rows,
        "list_query": list_query,
        "page_meta": page_meta,
        "activity_installation": selected,
        "activity_log": activities,
    }


def bulk_set_integrations_enabled(
    db: Session, connector_ids: list[str], *, enabled: bool
) -> int:
    changed: list[IntegrationInstallation] = []
    for installation_id in connector_ids:
        installation = installations.get_installation(db, installation_id)
        if enabled:
            static_result = installations.validate_static(
                db,
                installation_id=installation.id,
                actor="admin.integrations.installed",
            )
            if not static_result.valid:
                raise ValueError(
                    "Static validation failed: " + ", ".join(static_result.error_codes)
                )
            if not installation.capability_bindings:
                raise ValueError("Installation has no capability bindings")
            context = build_execution_context(
                db,
                capability_binding_id=installation.capability_bindings[0].id,
                allow_disabled=True,
            )
            installations.enable_after_connection_validation(
                db,
                installation_id=installation.id,
                connection_result=validate_connection(context),
                actor="admin.integrations.installed",
            )
        else:
            installations.disable_installation(
                db,
                installation_id=installation.id,
                reason="operator_disabled",
                actor="admin.integrations.installed",
            )
        changed.append(installation)
    if changed:
        installations.commit_installation_changes(db, changed[-1])
    return len(changed)


def uninstall_integration(db: Session, connector_id: str):
    installation = installations.retire_installation(
        db,
        installation_id=connector_id,
        reason="operator_retired",
        actor="admin.integrations.installed",
    )
    return installations.commit_installation_changes(db, installation)


def create_connector(db: Session, command: ConnectorFormCommand):
    if not command.name or not command.name.strip() or not command.connector_type:
        raise ValueError("Name and connector type are required.")
    if len(command.name.strip()) > 160:
        raise ValueError("Connection name must be 160 characters or fewer.")
    if (
        db.query(ConnectorConfig.id)
        .filter(ConnectorConfig.name == command.name.strip())
        .first()
    ):
        raise ValueError("A legacy connection with this name already exists.")
    auth = _auth_fields(command)
    _validate_auth_fields(command.auth_type, auth)
    if command.timeout_sec is not None and command.timeout_sec < 1:
        raise ValueError("Timeout must be at least one second.")
    payload = ConnectorConfigCreate(
        name=command.name.strip(),
        connector_type=command.connector_type,
        auth_type=command.auth_type,
        base_url=_http_base_url(command.base_url),
        timeout_sec=command.timeout_sec,
        auth_config=auth or None,
        headers=_header_fields(command) or None,
        retry_policy=_retry_fields(command) or None,
        notes=command.notes.strip() if command.notes else None,
        is_active=command.is_active,
    )
    return connector_service.connector_configs.create(db, payload)


SECRET_VALUE_SENTINEL = "__redacted__"


def _unmask_secret_values(submitted: object, stored: dict | None) -> object:
    """Restore sentinel values in a submitted dict from the stored original.

    Lets the edit form show masked secrets and keep them on save unless the
    operator types a new value over the mask.
    """
    if not isinstance(submitted, dict):
        return submitted
    stored = stored if isinstance(stored, dict) else {}
    restored = {}
    for key, value in submitted.items():
        if value == SECRET_VALUE_SENTINEL:
            if key not in stored:
                raise ValueError(f"Enter a value for header {key}.")
            restored[key] = stored[key]
        else:
            restored[key] = value
    return restored


def update_connector_config(
    db: Session,
    connector_id: str,
    command: ConnectorFormCommand,
):
    existing = connector_service.connector_configs.get(db, connector_id)
    auth = _auth_fields(command)
    if command.auth_type != existing.auth_type:
        _validate_auth_fields(command.auth_type, auth)
    if command.timeout_sec is not None and command.timeout_sec < 1:
        raise ValueError("Timeout must be at least one second.")
    data: dict[str, object] = {
        "auth_type": command.auth_type,
        "timeout_sec": command.timeout_sec,
        "headers": _unmask_secret_values(_header_fields(command), existing.headers),
        "notes": command.notes.strip() if command.notes else None,
        "is_active": command.is_active,
    }
    if command.base_url and command.base_url.strip():
        data["base_url"] = _http_base_url(command.base_url)
    elif command.clear_base_url:
        data["base_url"] = None
    if auth or command.auth_type != existing.auth_type:
        data["auth_config"] = auth or None
    retry = dict(existing.retry_policy or {})
    for key in ("max_attempts", "backoff_sec"):
        retry.pop(key, None)
    retry.update(_retry_fields(command))
    data["retry_policy"] = retry or None
    payload = ConnectorConfigUpdate.model_validate(data)
    return connector_service.connector_configs.update(db, connector_id, payload)


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
    notes: str | None,
    is_active: bool,
):
    from app.models.integration import IntegrationTargetType

    payload = IntegrationTargetCreate(
        name=name.strip(),
        target_type=validate_enum(target_type, IntegrationTargetType, "target_type"),
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
    capability_binding_id: str,
    entity_type: str | None = None,
    direction: str | None = None,
    trigger_mode: str | None = None,
    mapping_config: str | None = None,
    filter_config: str | None = None,
    conflict_policy: str | None = None,
    notes: str | None,
    is_active: bool,
):
    from app.models.integration import IntegrationJobType, IntegrationScheduleType

    interval_value = int(interval_minutes) if interval_minutes else None
    if schedule_type == "interval" and not interval_value:
        raise ValueError("interval_minutes is required for interval schedules")
    payload = IntegrationJobCreate(
        target_id=cast(UUID, parse_uuid(target_id, "target_id")),
        capability_binding_id=cast(
            UUID, parse_uuid(capability_binding_id, "capability_binding_id")
        ),
        name=name.strip(),
        job_type=validate_enum(job_type, IntegrationJobType, "job_type"),
        schedule_type=validate_enum(
            schedule_type, IntegrationScheduleType, "schedule_type"
        ),
        interval_minutes=interval_value,
        entity_type=(entity_type or "").strip() or None,
        direction=(direction or "").strip() or None,
        trigger_mode=(trigger_mode or "").strip() or None,
        mapping_config=_parse_json(mapping_config, "mapping_config"),
        filter_config=_parse_json(filter_config, "filter_config"),
        conflict_policy=(conflict_policy or "").strip() or None,
        notes=notes.strip() if notes else None,
        is_active=is_active,
    )
    return integration_service.integration_jobs.create(db, payload)
