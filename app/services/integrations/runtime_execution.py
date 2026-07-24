"""Version-pinned connector runtime selection and secret materialization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallationState,
)
from app.services.integrations.connectors.dotmac_crm import (
    DotmacCrmRunner,
    RuntimeCrmObservationSource,
)
from app.services.integrations.connectors.dotmac_erp import DotmacErpRunner
from app.services.integrations.connectors.http_webhook import HttpWebhookRunner
from app.services.integrations.connectors.lead_capture_http import (
    LeadCaptureHttpRunner,
)
from app.services.integrations.connectors.payment_gateway import PaymentGatewayRunner
from app.services.integrations.connectors.whatsapp_runtime import WhatsAppRuntimeRunner
from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.registry import require_pinned_connector_definition
from app.services.integrations.runtime import (
    ConnectorRunner,
    OperationEnvelope,
    OperationResult,
    OperationTrigger,
    RunnerRegistry,
    ValidationResult,
)
from app.services.secrets import resolve_secret


class RuntimeExecutionError(RuntimeError):
    """Raised before dispatch when a pinned runtime contract is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeExecutionContext:
    binding: IntegrationCapabilityBinding
    manifest: ConnectorManifest
    config: Mapping[str, Any]
    secret_material: Mapping[str, str] = field(repr=False)
    runner: ConnectorRunner = field(repr=False)


def default_runner_registry() -> RunnerRegistry:
    registry = RunnerRegistry()
    registry.register("webhook.http", HttpWebhookRunner())
    registry.register("lead.capture.http", LeadCaptureHttpRunner())
    registry.register("dotmac.crm", DotmacCrmRunner())
    registry.register("dotmac.erp", DotmacErpRunner())
    registry.register("paystack", PaymentGatewayRunner("paystack"))
    registry.register("flutterwave", PaymentGatewayRunner("flutterwave"))
    registry.register("whatsapp", WhatsAppRuntimeRunner())
    return registry


def build_execution_context(
    db: Session,
    *,
    capability_binding_id: UUID,
    allow_disabled: bool = False,
    runner_registry: RunnerRegistry | None = None,
    runner_override: ConnectorRunner | None = None,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
) -> RuntimeExecutionContext:
    binding = db.get(IntegrationCapabilityBinding, capability_binding_id)
    if binding is None:
        raise RuntimeExecutionError("capability binding not found")
    installation = binding.installation
    if installation.state in {
        IntegrationInstallationState.quarantined.value,
        IntegrationInstallationState.retired.value,
    }:
        raise RuntimeExecutionError(
            f"installation is not executable: {installation.state}"
        )
    if not allow_disabled and (
        installation.state != IntegrationInstallationState.enabled.value
        or binding.state != IntegrationBindingState.enabled.value
    ):
        raise RuntimeExecutionError("installation capability is not enabled")
    revision = installation.current_config_revision
    if revision is None:
        raise RuntimeExecutionError("current configuration revision is missing")
    try:
        manifest = require_pinned_connector_definition(
            installation.connector_key,
            version=installation.connector_version,
            manifest_digest=installation.manifest_digest,
        )
    except KeyError as exc:
        raise RuntimeExecutionError(
            "connector manifest pin is not available in this deployment"
        ) from exc
    if manifest.capability(binding.capability_id) is None:
        raise RuntimeExecutionError("binding capability is not declared")

    material: dict[str, str] = {}
    for name, reference in dict(revision.secret_refs or {}).items():
        resolved = secret_resolver(str(reference))
        if not resolved:
            raise RuntimeExecutionError(f"secret binding could not be resolved: {name}")
        material[str(name)] = str(resolved)
    runner = runner_override or (runner_registry or default_runner_registry()).resolve(
        installation.connector_key
    )
    return RuntimeExecutionContext(
        binding=binding,
        manifest=manifest,
        config=dict(revision.config_json or {}),
        secret_material=material,
        runner=runner,
    )


def validate_connection(context: RuntimeExecutionContext) -> ValidationResult:
    return context.runner.validate(
        manifest=context.manifest,
        config=context.config,
        secret_material=context.secret_material,
    )


def make_operation_executor(
    context: RuntimeExecutionContext,
    *,
    correlation_id: str,
    trigger: OperationTrigger,
    actor: str | None = None,
    timeout_seconds: int = 45,
) -> Callable[[str, dict[str, Any]], OperationResult]:
    installation = context.binding.installation
    revision = installation.current_config_revision
    if revision is None:  # defensive; context construction already enforces this
        raise RuntimeExecutionError("current configuration revision is missing")

    def execute(action: str, params: dict[str, Any]) -> OperationResult:
        identity_payload = json.dumps(
            {
                "action": action,
                "binding": str(context.binding.id),
                "correlation": correlation_id,
                "params": params,
                "revision": str(revision.id),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
        idempotency_key = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
        envelope = OperationEnvelope(
            operation_id=uuid5(NAMESPACE_URL, f"dotmac-integration:{idempotency_key}"),
            correlation_id=correlation_id,
            installation_id=installation.id,
            capability_binding_id=context.binding.id,
            capability_id=context.binding.capability_id,
            connector_key=installation.connector_key,
            connector_version=installation.connector_version,
            manifest_digest=installation.manifest_digest,
            config_revision_id=revision.id,
            trigger=trigger,
            idempotency_key=idempotency_key,
            deadline_at=datetime.now(UTC) + timedelta(seconds=max(1, timeout_seconds)),
            payload={"action": action, "params": params},
            actor=actor,
        )
        return context.runner.execute(
            envelope,
            config=context.config,
            secret_material=context.secret_material,
        )

    return execute


def crm_observation_source(
    context: RuntimeExecutionContext,
    *,
    correlation_id: str,
    trigger: OperationTrigger,
    actor: str | None = None,
) -> RuntimeCrmObservationSource:
    return RuntimeCrmObservationSource(
        make_operation_executor(
            context,
            correlation_id=correlation_id,
            trigger=trigger,
            actor=actor,
        )
    )
