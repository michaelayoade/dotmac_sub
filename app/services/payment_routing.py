"""Canonical online-payment gateway presentment and checkout provenance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import PaymentProvider, PaymentProviderType, TopupIntent
from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallation,
    IntegrationInstallationState,
)
from app.services.integrations import installations
from app.services.integrations.connectors.payment_gateway import (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_RECONCILE_CAPABILITY,
    PAYMENT_REFUND_CAPABILITY,
    PAYMENT_WEBHOOK_CAPABILITY,
)

SUPPORTED_PROVIDER_TYPES: tuple[PaymentProviderType, ...] = (
    PaymentProviderType.paystack,
    PaymentProviderType.flutterwave,
)


@dataclass(frozen=True)
class GatewayOption:
    provider_type: PaymentProviderType
    provider_id: UUID
    installation_id: UUID
    capability_binding_id: UUID
    presentment_priority: int


class GatewayHealthState(StrEnum):
    """Closed operator-facing gateway availability states."""

    healthy = "healthy"
    ambiguous = "ambiguous"
    not_configured = "not_configured"
    not_installed = "not_installed"
    checkout_disabled = "checkout_disabled"
    disabled = "disabled"
    misconfigured = "misconfigured"


@dataclass(frozen=True)
class GatewayHealth:
    """Typed gateway setup and checkout-health projection."""

    provider_type: PaymentProviderType
    provider_name: str
    provider_id: UUID | None
    configured: bool
    active: bool
    capability_ready: bool
    lifecycle_ready: bool
    missing_capabilities: tuple[str, ...]
    installation_id: UUID | None
    capability_binding_id: UUID | None
    presentment_priority: int
    health: GatewayHealthState
    health_label: str


def supported_provider_type_values() -> list[str]:
    return [item.value for item in SUPPORTED_PROVIDER_TYPES]


def parse_supported_provider_type(raw_value: str) -> PaymentProviderType:
    normalized = (raw_value or "").strip().lower()
    for provider_type in SUPPORTED_PROVIDER_TYPES:
        if provider_type.value == normalized:
            return provider_type
    allowed = ", ".join(supported_provider_type_values())
    raise ValueError(
        f"Unsupported payment provider type '{raw_value}'. Allowed: {allowed}"
    )


_REQUIRED_PROVIDER_CAPABILITIES = (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_WEBHOOK_CAPABILITY,
    PAYMENT_RECONCILE_CAPABILITY,
    PAYMENT_REFUND_CAPABILITY,
)


def _intent_binding(
    db: Session, provider_type: PaymentProviderType
) -> IntegrationCapabilityBinding | None:
    try:
        return installations.require_enabled_capability_binding(
            db,
            connector_key=provider_type.value,
            capability_id=PAYMENT_INTENT_CAPABILITY,
        )
    except installations.InstallationError:
        return None


def _missing_capabilities(
    db: Session,
    installation: IntegrationInstallation | None,
) -> list[str]:
    if installation is None:
        return list(_REQUIRED_PROVIDER_CAPABILITIES)
    enabled = set(
        db.scalars(
            select(IntegrationCapabilityBinding.capability_id).where(
                IntegrationCapabilityBinding.installation_id == installation.id,
                IntegrationCapabilityBinding.state
                == IntegrationBindingState.enabled.value,
            )
        ).all()
    )
    return [
        capability_id
        for capability_id in _REQUIRED_PROVIDER_CAPABILITIES
        if capability_id not in enabled
    ]


def _presentment_priority(binding: IntegrationCapabilityBinding | None) -> int:
    if binding is None:
        return 0
    raw = (binding.policy_json or {}).get("presentment_priority", 0)
    if isinstance(raw, bool):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def provider_health(db: Session) -> list[GatewayHealth]:
    rows: list[GatewayHealth] = []
    for provider_type in SUPPORTED_PROVIDER_TYPES:
        providers = list(
            db.scalars(
                select(PaymentProvider)
                .where(PaymentProvider.provider_type == provider_type)
                .order_by(PaymentProvider.created_at.asc(), PaymentProvider.id.asc())
            ).all()
        )
        intent_binding = _intent_binding(db, provider_type)
        installation = intent_binding.installation if intent_binding else None
        if installation is None:
            installation_rows = [
                row
                for row in installations.list_installations(
                    db,
                    connector_key=provider_type.value,
                    limit=200,
                )
                if row.state != IntegrationInstallationState.retired.value
            ]
            if len(installation_rows) == 1:
                installation = installation_rows[0]
        missing_capabilities = _missing_capabilities(db, installation)
        capability_ready = not missing_capabilities
        lifecycle_missing = [
            capability_id
            for capability_id in missing_capabilities
            if capability_id != PAYMENT_INTENT_CAPABILITY
        ]
        lifecycle_ready = bool(
            installation
            and installation.state == IntegrationInstallationState.enabled.value
            and not lifecycle_missing
        )
        if len(providers) > 1:
            health = GatewayHealthState.ambiguous
            health_label = "Multiple finance identities"
        elif not providers:
            health = GatewayHealthState.not_configured
            health_label = "Finance identity missing"
        elif installation is None:
            health = GatewayHealthState.not_installed
            health_label = "Gateway not installed"
        elif (
            installation.state == IntegrationInstallationState.enabled.value
            and intent_binding is None
            and lifecycle_ready
        ):
            health = GatewayHealthState.checkout_disabled
            health_label = "New checkout disabled"
        elif installation.state != IntegrationInstallationState.enabled.value:
            health = GatewayHealthState.disabled
            health_label = "Gateway not enabled"
        elif not capability_ready:
            health = GatewayHealthState.misconfigured
            health_label = "Capability bundle incomplete"
        else:
            health = GatewayHealthState.healthy
            health_label = "Healthy"
        provider = providers[0] if providers else None
        rows.append(
            GatewayHealth(
                provider_type=provider_type,
                provider_name=(
                    provider.name if provider else provider_type.value.title()
                ),
                provider_id=provider.id if provider else None,
                configured=bool(providers),
                active=bool(
                    installation
                    and installation.state == IntegrationInstallationState.enabled.value
                ),
                capability_ready=capability_ready,
                lifecycle_ready=lifecycle_ready,
                missing_capabilities=tuple(missing_capabilities),
                installation_id=installation.id if installation else None,
                capability_binding_id=intent_binding.id if intent_binding else None,
                presentment_priority=_presentment_priority(intent_binding),
                health=health,
                health_label=health_label,
            )
        )
    return rows


def gateway_options(db: Session) -> list[GatewayOption]:
    routes: list[GatewayOption] = []
    for row in provider_health(db):
        if (
            row.health is GatewayHealthState.healthy
            and row.provider_id
            and row.installation_id
            and row.capability_binding_id
        ):
            routes.append(
                GatewayOption(
                    provider_type=row.provider_type,
                    provider_id=row.provider_id,
                    installation_id=row.installation_id,
                    capability_binding_id=row.capability_binding_id,
                    presentment_priority=row.presentment_priority,
                )
            )
    return sorted(
        routes,
        key=lambda route: (-route.presentment_priority, route.provider_type.value),
    )


def select_checkout_provider(
    db: Session, requested: str | None = None
) -> GatewayOption:
    routes = gateway_options(db)
    if requested:
        requested_type = parse_supported_provider_type(requested)
        match = next(
            (route for route in routes if route.provider_type == requested_type), None
        )
        if match is None:
            raise ValueError(
                f"{requested_type.value.title()} is not available for new payments"
            )
        return match
    if routes:
        return routes[0]
    raise ValueError("No online payment provider is currently available")


def provider_for_intent(
    intent: TopupIntent, asserted_provider: str | None = None
) -> PaymentProviderType:
    provider_type = parse_supported_provider_type(str(intent.provider_type or ""))
    if asserted_provider:
        asserted = parse_supported_provider_type(asserted_provider)
        if asserted != provider_type:
            raise ValueError("Payment provider does not match the original checkout")
    return provider_type
