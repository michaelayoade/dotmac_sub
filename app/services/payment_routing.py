"""Canonical payment-provider routing and checkout provenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import PaymentProvider, PaymentProviderType, TopupIntent
from app.models.domain_settings import SettingDomain
from app.schemas.billing import PaymentProviderCreate, PaymentProviderUpdate
from app.services import settings_spec
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
_BOOL_TRUE_VALUES = {"1", "true", "yes", "on"}


def create_configured_provider(
    db: Session,
    command: PaymentProviderCreate,
) -> PaymentProvider:
    """Persist one provider configuration for the legacy routing owner."""

    provider = PaymentProvider(
        name=command.name,
        provider_type=command.provider_type,
        is_active=command.is_active,
        notes=command.notes,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def update_configured_provider(
    db: Session,
    provider: PaymentProvider,
    command: PaymentProviderUpdate,
) -> PaymentProvider:
    """Apply the explicit provider configuration fields supplied by an adapter."""

    fields = command.model_fields_set
    if "name" in fields:
        assert command.name is not None
        provider.name = command.name
    if "provider_type" in fields:
        assert command.provider_type is not None
        provider.provider_type = command.provider_type
    if "is_active" in fields:
        assert command.is_active is not None
        provider.is_active = command.is_active
    if "notes" in fields:
        provider.notes = command.notes
    db.commit()
    db.refresh(provider)
    return provider


def deactivate_configured_provider(db: Session, provider: PaymentProvider) -> None:
    """Soft-deactivate one configured provider."""

    provider.is_active = False
    db.commit()


@dataclass(frozen=True)
class ProviderRoute:
    provider_type: PaymentProviderType
    provider_id: str


@dataclass(frozen=True)
class PaymentRoutingPolicy:
    primary: PaymentProviderType
    secondary: PaymentProviderType
    failover_enabled: bool


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


def _setting(db: Session, key: str) -> Any:
    return settings_spec.resolve_value(db, SettingDomain.billing, key)


def _string_setting(db: Session, key: str, default: str) -> str:
    raw = _setting(db, key)
    value = str(raw or "").strip()
    return value or default


def _bool_setting(db: Session, key: str, default: bool) -> bool:
    raw = _setting(db, key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _BOOL_TRUE_VALUES


def get_routing_policy(db: Session) -> PaymentRoutingPolicy:
    primary = parse_supported_provider_type(
        _string_setting(db, "payment_gateway_primary_provider", "paystack")
    )
    secondary = parse_supported_provider_type(
        _string_setting(db, "payment_gateway_secondary_provider", "flutterwave")
    )
    if primary == secondary:
        secondary = (
            PaymentProviderType.flutterwave
            if primary == PaymentProviderType.paystack
            else PaymentProviderType.paystack
        )
    return PaymentRoutingPolicy(
        primary=primary,
        secondary=secondary,
        failover_enabled=_bool_setting(db, "payment_gateway_failover_enabled", True),
    )


_REQUIRED_PROVIDER_CAPABILITIES = (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_WEBHOOK_CAPABILITY,
    PAYMENT_RECONCILE_CAPABILITY,
    PAYMENT_REFUND_CAPABILITY,
)


def _missing_capabilities(db: Session, provider_type: PaymentProviderType) -> list[str]:
    missing: list[str] = []
    for capability_id in _REQUIRED_PROVIDER_CAPABILITIES:
        try:
            installations.require_enabled_capability_binding(
                db,
                connector_key=provider_type.value,
                capability_id=capability_id,
            )
        except installations.InstallationError:
            missing.append(capability_id)
    return missing


def provider_health(db: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_type in SUPPORTED_PROVIDER_TYPES:
        providers = list(
            db.scalars(
                select(PaymentProvider)
                .where(PaymentProvider.provider_type == provider_type)
                .order_by(PaymentProvider.created_at.asc(), PaymentProvider.id.asc())
            ).all()
        )
        active = [provider for provider in providers if provider.is_active]
        missing_capabilities = _missing_capabilities(db, provider_type)
        capability_ready = not missing_capabilities
        if len(active) > 1:
            health = "ambiguous"
            health_label = "Multiple Active Providers"
        elif not providers:
            health = "not_configured"
            health_label = "Not Configured"
        elif not active:
            health = "inactive"
            health_label = "Inactive"
        elif not capability_ready:
            health = "misconfigured"
            health_label = "Integration Not Ready"
        else:
            health = "healthy"
            health_label = "Healthy"
        provider = (
            active[0] if len(active) == 1 else providers[0] if providers else None
        )
        rows.append(
            {
                "provider_type": provider_type.value,
                "provider_name": provider.name
                if provider
                else provider_type.value.title(),
                "provider_id": str(provider.id) if provider else None,
                "configured": bool(providers),
                "active": len(active) == 1,
                "capability_ready": capability_ready,
                "missing_capabilities": missing_capabilities,
                "health": health,
                "health_label": health_label,
            }
        )
    return rows


def eligible_routes(db: Session) -> list[ProviderRoute]:
    health_by_type = {row["provider_type"]: row for row in provider_health(db)}
    policy = get_routing_policy(db)
    ordered = [policy.primary, policy.secondary]
    routes: list[ProviderRoute] = []
    for provider_type in ordered:
        row = health_by_type[provider_type.value]
        if row["health"] == "healthy" and row["provider_id"]:
            routes.append(
                ProviderRoute(
                    provider_type=provider_type, provider_id=row["provider_id"]
                )
            )
    return routes


def select_checkout_provider(
    db: Session, requested: str | None = None
) -> ProviderRoute:
    routes = eligible_routes(db)
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
    policy = get_routing_policy(db)
    primary = next(
        (route for route in routes if route.provider_type == policy.primary), None
    )
    if primary is not None:
        return primary
    if policy.failover_enabled:
        secondary = next(
            (route for route in routes if route.provider_type == policy.secondary), None
        )
        if secondary is not None:
            return secondary
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
