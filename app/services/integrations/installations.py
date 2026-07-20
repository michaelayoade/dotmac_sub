"""Canonical additive owner for connector installation configuration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationConfigRevision,
    IntegrationInstallation,
    IntegrationInstallationState,
    IntegrationValidationStatus,
)
from app.services.common import coerce_uuid
from app.services.integrations.connectors.http_webhook import validate_https_url
from app.services.integrations.manifest import (
    ConnectorManifest,
    ConnectorRuntimeType,
)
from app.services.integrations.registry import require_connector_definition
from app.services.integrations.runtime import ValidationResult
from app.services.secrets import is_secret_ref


class InstallationError(ValueError):
    """Raised when an installation violates its manifest or lifecycle."""


def config_revision_digest(
    *,
    config: dict[str, Any],
    secret_refs: dict[str, str],
    schema_version: str,
) -> str:
    payload = json.dumps(
        {
            "config": config,
            "schema_version": schema_version,
            "secret_refs": secret_refs,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_installation(
    db: Session, installation_id: UUID | str
) -> IntegrationInstallation:
    installation = db.get(IntegrationInstallation, coerce_uuid(str(installation_id)))
    if installation is None:
        raise InstallationError("integration installation not found")
    return installation


def commit_installation_changes(
    db: Session, installation: IntegrationInstallation
) -> IntegrationInstallation:
    """Commit one application-service unit of work owned by installations."""

    db.commit()
    db.refresh(installation)
    return installation


def list_installations(
    db: Session,
    *,
    connector_key: str | None = None,
    state: IntegrationInstallationState | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[IntegrationInstallation]:
    query = db.query(IntegrationInstallation)
    if connector_key:
        query = query.filter(
            IntegrationInstallation.connector_key == connector_key.strip().lower()
        )
    if state is not None:
        query = query.filter(IntegrationInstallation.state == state.value)
    return (
        query.order_by(
            IntegrationInstallation.connector_key.asc(),
            IntegrationInstallation.name.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )


def require_enabled_capability_binding(
    db: Session,
    *,
    capability_id: str,
    connector_key: str | None = None,
) -> IntegrationCapabilityBinding:
    """Resolve one enabled binding, requiring an explicit default if ambiguous."""

    query = (
        db.query(IntegrationCapabilityBinding)
        .join(IntegrationInstallation)
        .filter(
            IntegrationCapabilityBinding.capability_id == capability_id,
            IntegrationCapabilityBinding.state == IntegrationBindingState.enabled.value,
            IntegrationInstallation.state == IntegrationInstallationState.enabled.value,
        )
    )
    if connector_key:
        query = query.filter(
            IntegrationInstallation.connector_key == connector_key.strip().lower()
        )
    bindings = query.order_by(IntegrationCapabilityBinding.created_at.asc()).all()
    if not bindings:
        raise InstallationError(f"no enabled binding for {capability_id}")
    if len(bindings) == 1:
        return bindings[0]
    defaults = [
        binding
        for binding in bindings
        if (binding.policy_json or {}).get("default") is True
    ]
    if len(defaults) != 1:
        raise InstallationError(
            f"multiple enabled bindings for {capability_id}; exactly one must be default"
        )
    return defaults[0]


def create_draft(
    db: Session,
    *,
    connector_key: str,
    name: str,
    environment: str = "production",
    actor: str | None = None,
) -> IntegrationInstallation:
    definition = require_connector_definition(connector_key)
    if definition.runtime.type == ConnectorRuntimeType.catalogue_only:
        raise InstallationError(
            f"connector {definition.key} has no approved executable runtime"
        )
    normalized_name = name.strip()
    if not normalized_name:
        raise InstallationError("installation name is required")
    if environment not in {"production", "sandbox", "test"}:
        raise InstallationError("invalid installation environment")

    installation = IntegrationInstallation(
        connector_key=definition.key,
        connector_version=definition.version,
        manifest_digest=definition.digest,
        name=normalized_name,
        environment=environment,
        state=IntegrationInstallationState.draft.value,
        created_by=actor,
        updated_by=actor,
    )
    db.add(installation)
    db.flush()
    return installation


def create_config_revision(
    db: Session,
    *,
    installation_id: UUID | str,
    config: dict[str, Any] | None = None,
    secret_refs: dict[str, str] | None = None,
    schema_version: str = "v1",
    actor: str | None = None,
) -> IntegrationConfigRevision:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation cannot be configured")
    definition = _definition_for_installation(installation)
    normalized_config = dict(config or {})
    normalized_secret_refs = {
        str(name): str(reference) for name, reference in dict(secret_refs or {}).items()
    }
    _validate_secret_refs(definition, normalized_secret_refs)
    shape_errors = validate_config_shape(
        normalized_config,
        definition.config_schema,
    )
    if shape_errors:
        raise InstallationError("; ".join(shape_errors))
    normalized_schema_version = schema_version.strip()
    if not normalized_schema_version:
        raise InstallationError("configuration schema version is required")

    digest = config_revision_digest(
        config=normalized_config,
        secret_refs=normalized_secret_refs,
        schema_version=normalized_schema_version,
    )
    existing = (
        db.query(IntegrationConfigRevision)
        .filter(
            IntegrationConfigRevision.installation_id == installation.id,
            IntegrationConfigRevision.config_digest == digest,
        )
        .one_or_none()
    )
    if existing is not None:
        installation.current_config_revision_id = existing.id
        installation.current_config_revision = existing
        installation.updated_by = actor
        db.flush()
        return existing

    next_revision = (
        int(
            db.query(func.max(IntegrationConfigRevision.revision))
            .filter(IntegrationConfigRevision.installation_id == installation.id)
            .scalar()
            or 0
        )
        + 1
    )
    revision = IntegrationConfigRevision(
        installation_id=installation.id,
        revision=next_revision,
        schema_version=normalized_schema_version,
        config_json=normalized_config,
        secret_refs=normalized_secret_refs,
        config_digest=digest,
        validation_status=IntegrationValidationStatus.pending.value,
        created_by=actor,
    )
    db.add(revision)
    db.flush()
    installation.current_config_revision_id = revision.id
    installation.current_config_revision = revision
    installation.state = IntegrationInstallationState.draft.value
    installation.state_reason = "configuration_changed"
    installation.validated_at = None
    installation.updated_by = actor
    for binding in installation.capability_bindings:
        binding.state = IntegrationBindingState.disabled.value
        binding.enabled_at = None
        binding.disabled_at = datetime.now(UTC)
        binding.updated_by = actor
    db.flush()
    return revision


def bind_capability(
    db: Session,
    *,
    installation_id: UUID | str,
    capability_id: str,
    scope: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    actor: str | None = None,
) -> IntegrationCapabilityBinding:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation cannot receive capabilities")
    definition = _definition_for_installation(installation)
    if definition.capability(capability_id) is None:
        raise InstallationError(
            f"connector {definition.key} does not declare {capability_id}"
        )
    binding = (
        db.query(IntegrationCapabilityBinding)
        .filter(
            IntegrationCapabilityBinding.installation_id == installation.id,
            IntegrationCapabilityBinding.capability_id == capability_id,
        )
        .one_or_none()
    )
    if binding is None:
        binding = IntegrationCapabilityBinding(
            installation=installation,
            capability_id=capability_id,
            state=IntegrationBindingState.disabled.value,
            created_by=actor,
        )
        db.add(binding)
    else:
        binding.state = IntegrationBindingState.disabled.value
        binding.enabled_at = None
        binding.disabled_at = datetime.now(UTC)
    binding.scope_json = dict(scope or {})
    binding.policy_json = dict(policy or {})
    binding.updated_by = actor
    installation.state = IntegrationInstallationState.draft.value
    installation.state_reason = "capability_binding_changed"
    installation.validated_at = None
    installation.updated_by = actor
    db.flush()
    return binding


def update_binding_policy(
    db: Session,
    *,
    capability_binding_id: UUID | str,
    policy: dict[str, Any],
    actor: str | None = None,
) -> IntegrationCapabilityBinding:
    """Replace an enabled binding's operator-owned dispatch policy."""

    binding = db.get(
        IntegrationCapabilityBinding,
        coerce_uuid(str(capability_binding_id)),
    )
    if binding is None:
        raise InstallationError("integration capability binding not found")
    if (
        binding.installation.state != IntegrationInstallationState.enabled.value
        or binding.state != IntegrationBindingState.enabled.value
    ):
        raise InstallationError("capability binding must be enabled")
    binding.policy_json = dict(policy)
    binding.updated_by = actor
    db.flush()
    return binding


def validate_static(
    db: Session,
    *,
    installation_id: UUID | str,
    actor: str | None = None,
) -> ValidationResult:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation cannot be validated")
    installation.state = IntegrationInstallationState.validating.value
    installation.updated_by = actor
    db.flush()

    errors = _static_validation_errors(installation)
    revision = installation.current_config_revision
    if errors:
        installation.state = IntegrationInstallationState.draft.value
        installation.state_reason = ",".join(errors)
        installation.validated_at = None
        if revision is not None:
            revision.validation_status = IntegrationValidationStatus.invalid.value
            revision.validation_errors = list(errors)
    else:
        installation.state = IntegrationInstallationState.disabled.value
        installation.state_reason = "connection_validation_required"
        installation.validated_at = datetime.now(UTC)
        installation.disabled_at = installation.disabled_at or datetime.now(UTC)
        if revision is not None:
            revision.validation_status = IntegrationValidationStatus.valid.value
            revision.validation_errors = None
    db.flush()
    return ValidationResult(valid=not errors, error_codes=tuple(errors))


def enable_after_connection_validation(
    db: Session,
    *,
    installation_id: UUID | str,
    connection_result: ValidationResult,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = get_installation(db, installation_id)
    static_errors = _static_validation_errors(installation)
    if static_errors:
        raise InstallationError(
            "installation static validation failed: " + ",".join(static_errors)
        )
    if not connection_result.valid:
        raise InstallationError(
            "connector connection validation failed: "
            + ",".join(connection_result.error_codes)
        )
    installation.state = IntegrationInstallationState.enabled.value
    installation.state_reason = None
    installation.validated_at = datetime.now(UTC)
    installation.enabled_at = datetime.now(UTC)
    installation.disabled_at = None
    installation.updated_by = actor
    for binding in installation.capability_bindings:
        binding.state = IntegrationBindingState.enabled.value
        binding.enabled_at = datetime.now(UTC)
        binding.disabled_at = None
        binding.updated_by = actor
    db.flush()
    return installation


def disable_installation(
    db: Session,
    *,
    installation_id: UUID | str,
    reason: str,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = get_installation(db, installation_id)
    if installation.state == IntegrationInstallationState.retired.value:
        raise InstallationError("retired installation is already terminal")
    installation.state = IntegrationInstallationState.disabled.value
    installation.state_reason = reason.strip() or "operator_disabled"
    installation.enabled_at = None
    installation.disabled_at = datetime.now(UTC)
    installation.updated_by = actor
    for binding in installation.capability_bindings:
        binding.state = IntegrationBindingState.disabled.value
        binding.enabled_at = None
        binding.disabled_at = datetime.now(UTC)
        binding.updated_by = actor
    db.flush()
    return installation


def quarantine_installation(
    db: Session,
    *,
    installation_id: UUID | str,
    reason: str,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = disable_installation(
        db,
        installation_id=installation_id,
        reason=reason,
        actor=actor,
    )
    installation.state = IntegrationInstallationState.quarantined.value
    installation.quarantined_at = datetime.now(UTC)
    db.flush()
    return installation


def retire_installation(
    db: Session,
    *,
    installation_id: UUID | str,
    reason: str,
    actor: str | None = None,
) -> IntegrationInstallation:
    installation = get_installation(db, installation_id)
    if installation.state != IntegrationInstallationState.retired.value:
        if installation.state != IntegrationInstallationState.quarantined.value:
            disable_installation(
                db,
                installation_id=installation.id,
                reason=reason,
                actor=actor,
            )
        installation.state = IntegrationInstallationState.retired.value
        installation.state_reason = reason.strip() or "operator_retired"
        installation.retired_at = datetime.now(UTC)
        installation.updated_by = actor
        db.flush()
    return installation


def validate_config_shape(
    config: dict[str, Any], schema: dict[str, Any]
) -> tuple[str, ...]:
    """Validate the safe subset used by manifest-driven admin forms."""

    if not schema:
        return ()
    errors: list[str] = []
    if schema.get("type", "object") != "object":
        return ("config_schema_type_must_be_object",)
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return ("config_schema_properties_invalid",)
    required = schema.get("required") or []
    if not isinstance(required, list):
        return ("config_schema_required_invalid",)
    for key in required:
        if key not in config:
            errors.append(f"config_required:{key}")
    if schema.get("additionalProperties") is False:
        for key in sorted(set(config) - set(properties)):
            errors.append(f"config_unknown:{key}")
    type_map: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in config.items():
        spec = properties.get(key)
        if not isinstance(spec, dict) or value is None:
            continue
        expected_name = spec.get("type")
        expected = type_map.get(str(expected_name))
        is_bool_number = isinstance(value, bool) and expected_name in {
            "integer",
            "number",
        }
        if expected is not None and (not isinstance(value, expected) or is_bool_number):
            errors.append(f"config_type:{key}:{expected_name}")
            continue
        allowed = spec.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            errors.append(f"config_enum:{key}")
    return tuple(errors)


def _definition_for_installation(
    installation: IntegrationInstallation,
) -> ConnectorManifest:
    definition = require_connector_definition(installation.connector_key)
    if installation.connector_version != definition.version:
        raise InstallationError("installed connector version is not deployed")
    if installation.manifest_digest != definition.digest:
        raise InstallationError("installed manifest digest differs from deployment")
    return definition


def _validate_secret_refs(
    definition: ConnectorManifest,
    secret_refs: dict[str, str],
) -> None:
    declared = {binding.name for binding in definition.secrets}
    unknown = sorted(set(secret_refs) - declared)
    if unknown:
        raise InstallationError("undeclared secret binding(s): " + ",".join(unknown))
    invalid = sorted(
        name for name, reference in secret_refs.items() if not is_secret_ref(reference)
    )
    if invalid:
        raise InstallationError(
            "secret bindings must store references only: " + ",".join(invalid)
        )


def _static_validation_errors(
    installation: IntegrationInstallation,
) -> list[str]:
    try:
        definition = _definition_for_installation(installation)
    except (InstallationError, KeyError):
        return ["definition_mismatch"]
    revision = installation.current_config_revision
    if revision is None:
        return ["config_revision_missing"]
    errors = list(validate_config_shape(revision.config_json, definition.config_schema))
    secret_refs = {
        str(name): str(reference)
        for name, reference in dict(revision.secret_refs or {}).items()
    }
    try:
        _validate_secret_refs(definition, secret_refs)
    except InstallationError:
        errors.append("secret_reference_invalid")
    missing_secrets = sorted(definition.required_secret_names - set(secret_refs))
    errors.extend(f"secret_required:{name}" for name in missing_secrets)
    if definition.capabilities and not installation.capability_bindings:
        errors.append("capability_binding_missing")
    for binding in installation.capability_bindings:
        if definition.capability(binding.capability_id) is None:
            errors.append(f"capability_undeclared:{binding.capability_id}")
    if definition.egress.allow_installation_hosts:
        host, egress_error = validate_https_url(revision.config_json.get("url"))
        if egress_error:
            errors.append(f"egress:{egress_error}")
        else:
            approved_hosts = {
                str(item).strip().lower().rstrip(".")
                for binding in installation.capability_bindings
                for item in (binding.policy_json or {}).get("approved_egress_hosts", [])
                if str(item).strip()
            }
            if host not in approved_hosts:
                errors.append(f"egress_host_not_approved:{host}")
    return errors
