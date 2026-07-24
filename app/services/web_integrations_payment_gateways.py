"""Admin projection and commands for installation-backed payment gateways."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.billing import PaymentProviderType
from app.models.integration_platform import IntegrationInstallation
from app.services import payment_gateway_finance, payment_routing
from app.services.integrations import installations
from app.services.integrations.connectors.payment_gateway import (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_WEBHOOK_CAPABILITY,
)
from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.registry import require_connector_definition
from app.services.integrations.runtime_execution import (
    build_execution_context,
    validate_connection,
)
from app.services.secrets import is_secret_ref, resolve_secret


def _provider_type(value: str) -> PaymentProviderType:
    return payment_routing.parse_supported_provider_type(value)


def _manifest(provider_type: PaymentProviderType) -> ConnectorManifest:
    definition = require_connector_definition(provider_type.value)
    if definition.connector_type != "payment":
        raise ValueError("Connector is not a payment gateway")
    if definition.capability(PAYMENT_INTENT_CAPABILITY) is None:
        raise ValueError("Payment connector has no checkout capability")
    return definition


def _manifest_default_config(definition: ConnectorManifest) -> dict[str, Any]:
    properties = dict(definition.config_schema.get("properties") or {})
    config = {
        str(name): schema["default"]
        for name, schema in properties.items()
        if isinstance(schema, dict) and "default" in schema
    }
    missing = [
        str(name)
        for name in definition.config_schema.get("required", [])
        if name not in config
    ]
    if missing:
        raise ValueError(
            "Payment connector manifest has no safe default for required config: "
            + ", ".join(missing)
        )
    return config


def _manifest_capabilities(definition: ConnectorManifest) -> tuple[str, ...]:
    return tuple(
        capability.id
        for capability in definition.capabilities
        if capability.id.startswith("payments.")
    )


def _required_refs(definition: ConnectorManifest) -> tuple[str, ...]:
    return tuple(binding.name for binding in definition.secrets if binding.required)


# Secrets a connector manifest marks optional that a bound capability cannot
# actually work without. `save_config` binds every declared payments.*
# capability, so an absent reference here yields an installation that passes
# static validation, reports healthy, and then fails silently in production:
# checkout cannot build a gateway context without a public key, and Flutterwave
# compares the inbound `verif-hash` header against a stored literal, so an
# absent hash rejects every webhook. Paystack is absent for the webhook case
# because it derives its expected signature from `gateway_credentials`.
_CAPABILITY_REQUIRED_SECRETS: dict[tuple[str, str], str] = {
    ("paystack", PAYMENT_INTENT_CAPABILITY): "public_key",
    ("flutterwave", PAYMENT_INTENT_CAPABILITY): "public_key",
    ("flutterwave", PAYMENT_WEBHOOK_CAPABILITY): "webhook_signing_secret",
}


def _capability_required_refs(
    definition: ConnectorManifest, capability_ids: tuple[str, ...]
) -> tuple[str, ...]:
    """Secret names the bound capabilities require despite being optional."""
    return tuple(
        sorted(
            {
                secret_name
                for (key, capability_id), secret_name in (
                    _CAPABILITY_REQUIRED_SECRETS.items()
                )
                if key == definition.key and capability_id in capability_ids
            }
        )
    )


def _reference_resolves(reference: str) -> bool:
    """Whether a well-formed reference actually yields a stored value."""
    try:
        return bool(resolve_secret(reference))
    except Exception:
        return False


def _selected_installation(
    db: Session, provider_type: PaymentProviderType
) -> IntegrationInstallation | None:
    rows = installations.list_installations(
        db, connector_key=provider_type.value, limit=200
    )
    rows = [row for row in rows if row.state != "retired"]
    if not rows:
        return None
    defaults = [
        row
        for row in rows
        if any(
            binding.capability_id == PAYMENT_INTENT_CAPABILITY
            and (binding.policy_json or {}).get("default") is True
            for binding in row.capability_bindings
        )
    ]
    if len(defaults) == 1:
        return defaults[0]
    if len(rows) == 1:
        return rows[0]
    raise ValueError(
        f"Multiple {provider_type.value.title()} installations exist without "
        "one default intent binding"
    )


def _masked_reference(reference: str) -> str:
    if not reference:
        return ""
    if len(reference) <= 14:
        return "*" * len(reference)
    return f"{reference[:6]}…{reference[-5:]}"


def _health_projection(
    health: payment_routing.GatewayHealth,
) -> dict[str, Any]:
    return {
        "provider_type": health.provider_type.value,
        "provider_name": health.provider_name,
        "provider_id": str(health.provider_id) if health.provider_id else None,
        "configured": health.configured,
        "active": health.active,
        "capability_ready": health.capability_ready,
        "lifecycle_ready": health.lifecycle_ready,
        "missing_capabilities": list(health.missing_capabilities),
        "installation_id": (
            str(health.installation_id) if health.installation_id else None
        ),
        "capability_binding_id": (
            str(health.capability_binding_id) if health.capability_binding_id else None
        ),
        "presentment_priority": health.presentment_priority,
        "health": health.health.value,
        "health_label": health.health_label,
    }


def build_config_state(db: Session, provider_type_value: str) -> dict[str, Any]:
    provider_type = _provider_type(provider_type_value)
    definition = _manifest(provider_type)
    installation = _selected_installation(db, provider_type)
    revision = installation.current_config_revision if installation else None
    refs = dict(revision.secret_refs or {}) if revision else {}
    intent_binding = next(
        (
            binding
            for binding in (installation.capability_bindings if installation else [])
            if binding.capability_id == PAYMENT_INTENT_CAPABILITY
        ),
        None,
    )
    priority = (
        (intent_binding.policy_json or {}).get("presentment_priority", 0)
        if intent_binding
        else 0
    )
    health = next(
        row
        for row in payment_routing.provider_health(db)
        if row.provider_type == provider_type
    )
    return {
        "provider_type": provider_type.value,
        "provider_label": provider_type.value.title(),
        "installation": installation,
        "health": _health_projection(health),
        "required_refs": _required_refs(definition),
        "form": {
            "presentment_priority": priority,
            "gateway_credentials": "",
            "gateway_credentials_masked": _masked_reference(
                str(refs.get("gateway_credentials") or "")
            ),
            "public_key": "",
            "public_key_masked": _masked_reference(str(refs.get("public_key") or "")),
            "webhook_signing_secret": "",
            "webhook_signing_secret_masked": _masked_reference(
                str(refs.get("webhook_signing_secret") or "")
            ),
        },
    }


def _secret_reference(
    candidate: str,
    existing: str | None,
    *,
    label: str,
    required: bool,
) -> str | None:
    value = candidate.strip()
    if not value:
        value = str(existing or "").strip()
    if not value:
        if required:
            raise ValueError(f"{label} secret reference is required")
        return None
    if not is_secret_ref(value):
        raise ValueError(f"{label} must be an OpenBao or environment reference")
    return value


def save_config(
    db: Session,
    *,
    provider_type_value: str,
    presentment_priority: int,
    gateway_credentials: str,
    public_key: str,
    webhook_signing_secret: str,
    actor: str = "admin.payment_gateway",
) -> IntegrationInstallation:
    provider_type = _provider_type(provider_type_value)
    definition = _manifest(provider_type)
    installation = _selected_installation(db, provider_type)
    current = installation.current_config_revision if installation else None
    existing_refs = dict(current.secret_refs or {}) if current else {}
    capability_ids = _manifest_capabilities(definition)
    required = set(_required_refs(definition)) | set(
        _capability_required_refs(definition, capability_ids)
    )
    secret_refs: dict[str, str] = {}
    for name, candidate, label in (
        ("gateway_credentials", gateway_credentials, "Gateway credential"),
        ("public_key", public_key, "Public key"),
        (
            "webhook_signing_secret",
            webhook_signing_secret,
            "Webhook signing secret",
        ),
    ):
        reference = _secret_reference(
            candidate,
            existing_refs.get(name),
            label=label,
            required=name in required,
        )
        if reference:
            secret_refs[name] = reference

    # Static validation only checks that a reference is well-formed, never that
    # it resolves. Creating a revision disables every existing binding, so an
    # unresolvable reference would pass validation here and take a working
    # gateway offline at live validation. Refuse before anything changes.
    unresolved = sorted(
        name
        for name, reference in secret_refs.items()
        if not _reference_resolves(reference)
    )
    if unresolved:
        raise ValueError(
            "These secret references do not resolve to a stored value: "
            + ", ".join(unresolved)
            + ". Create the value at that exact path and field before saving."
        )

    if installation is None:
        installation = installations.create_draft(
            db,
            connector_key=provider_type.value,
            name=f"{provider_type.value.title()} Production",
            environment="production",
            actor=actor,
        )
    installations.create_config_revision(
        db,
        installation_id=installation.id,
        config=_manifest_default_config(definition),
        secret_refs=secret_refs,
        actor=actor,
    )
    for capability_id in capability_ids:
        installations.bind_capability(
            db,
            installation_id=installation.id,
            capability_id=capability_id,
            policy={
                "default": True,
                **(
                    {"presentment_priority": presentment_priority}
                    if capability_id == PAYMENT_INTENT_CAPABILITY
                    else {}
                ),
            },
            actor=actor,
        )
    payment_gateway_finance.ensure_gateway_identity(db, provider_type=provider_type)
    installations.validate_static(db, installation_id=installation.id, actor=actor)
    return installation


def validate_and_enable(
    db: Session,
    *,
    provider_type_value: str,
    actor: str = "admin.payment_gateway",
) -> IntegrationInstallation:
    provider_type = _provider_type(provider_type_value)
    installation = _selected_installation(db, provider_type)
    if installation is None:
        raise ValueError("Save the gateway configuration before enabling it")
    intent_binding = next(
        (
            binding
            for binding in installation.capability_bindings
            if binding.capability_id == PAYMENT_INTENT_CAPABILITY
        ),
        None,
    )
    if intent_binding is None:
        raise ValueError("Payment intent capability is not configured")
    context = build_execution_context(
        db,
        capability_binding_id=intent_binding.id,
        allow_disabled=True,
    )
    result = validate_connection(context)
    return installations.enable_after_connection_validation(
        db,
        installation_id=installation.id,
        connection_result=result,
        actor=actor,
    )


def disable(
    db: Session,
    *,
    provider_type_value: str,
    actor: str = "admin.payment_gateway",
) -> IntegrationInstallation:
    provider_type = _provider_type(provider_type_value)
    installation = _selected_installation(db, provider_type)
    if installation is None:
        raise ValueError("Payment gateway installation not found")
    intent_binding = next(
        (
            binding
            for binding in installation.capability_bindings
            if binding.capability_id == PAYMENT_INTENT_CAPABILITY
        ),
        None,
    )
    if intent_binding is None:
        raise ValueError("Payment intent capability is not configured")
    installations.disable_capability_binding(
        db,
        capability_binding_id=intent_binding.id,
        actor=actor,
    )
    return installation


__all__ = [
    "build_config_state",
    "disable",
    "save_config",
    "validate_and_enable",
]
