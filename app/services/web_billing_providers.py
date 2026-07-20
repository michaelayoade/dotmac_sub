"""Service helpers for billing payment-provider web routes."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func

from app.models.billing import (
    Payment,
    PaymentProviderEvent,
    PaymentProviderEventStatus,
    PaymentProviderType,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.billing import PaymentProviderCreate, PaymentProviderUpdate
from app.schemas.settings import DomainSettingUpdate
from app.services import billing as billing_service
from app.services import domain_settings as domain_settings_service
from app.services.payment_routing import (
    SUPPORTED_PROVIDER_TYPES,
    get_routing_policy,
    parse_supported_provider_type,
    provider_credentials,
    provider_health,
    supported_provider_type_values,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _provider_health_rows(db: Session) -> list[dict[str, Any]]:
    rows = provider_health(db)
    for row in rows:
        provider_id = row.pop("provider_id", None)
        failed_events = 0
        last_event_at = None
        if provider_id:
            failed_events = (
                db.query(func.count(PaymentProviderEvent.id))
                .filter(PaymentProviderEvent.provider_id == provider_id)
                .filter(
                    PaymentProviderEvent.status == PaymentProviderEventStatus.failed
                )
                .scalar()
                or 0
            )
            last_event_at = (
                db.query(func.max(PaymentProviderEvent.received_at))
                .filter(PaymentProviderEvent.provider_id == provider_id)
                .scalar()
            )
        row["failed_events"] = int(failed_events)
        row["last_event_at"] = last_event_at
    return rows


def get_failover_state(db: Session) -> dict[str, Any]:
    policy = get_routing_policy(db)
    return {
        "enabled": policy.failover_enabled,
        "primary": policy.primary.value,
        "secondary": policy.secondary.value,
        "options": supported_provider_type_values(),
    }


def update_failover_config(
    db: Session,
    *,
    failover_enabled: bool,
    primary_provider: str,
    secondary_provider: str,
) -> None:
    primary = parse_supported_provider_type(primary_provider)
    secondary = parse_supported_provider_type(secondary_provider)
    if primary == secondary:
        raise ValueError("Primary and secondary gateways must be different")
    billing_settings = domain_settings_service.billing_settings
    billing_settings.upsert_by_key(
        db,
        "payment_gateway_failover_enabled",
        DomainSettingUpdate(
            value_type=SettingValueType.boolean,
            value_text="true" if failover_enabled else "false",
            value_json=failover_enabled,
        ),
    )
    billing_settings.upsert_by_key(
        db,
        "payment_gateway_primary_provider",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=primary.value,
        ),
    )
    billing_settings.upsert_by_key(
        db,
        "payment_gateway_secondary_provider",
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=secondary.value,
        ),
    )


def run_provider_test(
    db: Session, *, provider_type_value: str, mode: str = "test"
) -> dict[str, Any]:
    provider_type = parse_supported_provider_type(provider_type_value)
    credentials = provider_credentials(db, provider_type)
    provider = billing_service.payment_providers.get_by_type(db, provider_type)
    errors: list[str] = []
    warnings: list[str] = []
    normalized_mode = (mode or "").strip().lower()
    if normalized_mode not in {"test", "live"}:
        normalized_mode = "test"
    if not provider:
        errors.append("Provider record has not been created")
    elif not provider.is_active:
        errors.append("Provider is inactive")

    secret_key = credentials.get("secret_key", "")
    public_key = credentials.get("public_key", "")
    if not secret_key:
        errors.append("Secret key is missing")
    if not public_key:
        errors.append("Public key is missing")

    if provider_type == PaymentProviderType.paystack:
        expected_secret_prefix = "sk_test_" if normalized_mode == "test" else "sk_live_"
        expected_public_prefix = "pk_test_" if normalized_mode == "test" else "pk_live_"
        if secret_key and not secret_key.startswith(expected_secret_prefix):
            warnings.append(f"Secret key prefix does not match {normalized_mode} mode")
        if public_key and not public_key.startswith(expected_public_prefix):
            warnings.append(f"Public key prefix does not match {normalized_mode} mode")
    else:
        secret_hash = credentials.get("secret_hash", "")
        if not secret_hash:
            errors.append("Webhook secret hash is missing")
        marker = "TEST" if normalized_mode == "test" else "LIVE"
        if secret_key and marker not in secret_key.upper():
            warnings.append(f"Secret key does not look like a {normalized_mode} key")
        if public_key and marker not in public_key.upper():
            warnings.append(f"Public key does not look like a {normalized_mode} key")

    ok = not errors
    message = (
        f"{provider_type.value.title()} {normalized_mode} configuration is valid"
        if ok
        else f"{provider_type.value.title()} {normalized_mode} configuration failed checks"
    )
    return {
        "ok": ok,
        "provider_type": provider_type.value,
        "mode": normalized_mode,
        "message": message,
        "errors": errors,
        "warnings": warnings,
    }


def trigger_failover_if_needed(db: Session) -> tuple[bool, str]:
    failover = get_failover_state(db)
    if not failover["enabled"]:
        return False, "Automatic failover is disabled"

    health_rows = _provider_health_rows(db)
    by_type = {row["provider_type"]: row for row in health_rows}
    primary = str(failover["primary"])
    secondary = str(failover["secondary"])
    primary_health = str(by_type.get(primary, {}).get("health", "not_configured"))
    secondary_health = str(by_type.get(secondary, {}).get("health", "not_configured"))

    if primary_health == "healthy":
        return False, "Primary gateway is healthy; failover not required"
    if secondary_health != "healthy":
        return False, "Secondary gateway is not healthy; cannot fail over"

    update_failover_config(
        db,
        failover_enabled=True,
        primary_provider=secondary,
        secondary_provider=primary,
    )
    return (
        True,
        f"Failed over traffic to {secondary.title()} as the new primary gateway",
    )


def build_gateway_reconciliation(db: Session) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_missing_gateway = 0
    total_missing_dotmac = 0
    total_missing_references = 0
    for provider_type in SUPPORTED_PROVIDER_TYPES:
        provider = billing_service.payment_providers.get_by_type(db, provider_type)
        if not provider:
            rows.append(
                {
                    "provider_type": provider_type.value,
                    "provider_name": provider_type.value.title(),
                    "payment_count": 0,
                    "event_count": 0,
                    "matched_count": 0,
                    "missing_in_gateway": 0,
                    "missing_in_dotmac": 0,
                    "payments_missing_reference": 0,
                    "total_amount": 0.0,
                }
            )
            continue
        payments = (
            db.query(Payment)
            .filter(Payment.provider_id == provider.id)
            .filter(Payment.is_active.is_(True))
            .all()
        )
        events = (
            db.query(PaymentProviderEvent)
            .filter(PaymentProviderEvent.provider_id == provider.id)
            .all()
        )
        payment_refs = {
            str(item.external_id).strip()
            for item in payments
            if getattr(item, "external_id", None) and str(item.external_id).strip()
        }
        event_refs = {
            str(item.external_id).strip()
            for item in events
            if getattr(item, "external_id", None) and str(item.external_id).strip()
        }
        missing_in_gateway = len(payment_refs - event_refs)
        missing_in_dotmac = len(event_refs - payment_refs)
        payments_missing_reference = len(
            [
                item
                for item in payments
                if not str(getattr(item, "external_id", "") or "").strip()
            ]
        )
        matched_count = len(payment_refs & event_refs)
        total_amount = sum(
            Decimal(str(getattr(item, "amount", 0) or 0)) for item in payments
        )

        total_missing_gateway += missing_in_gateway
        total_missing_dotmac += missing_in_dotmac
        total_missing_references += payments_missing_reference
        rows.append(
            {
                "provider_type": provider_type.value,
                "provider_name": provider.name,
                "payment_count": len(payments),
                "event_count": len(events),
                "matched_count": matched_count,
                "missing_in_gateway": missing_in_gateway,
                "missing_in_dotmac": missing_in_dotmac,
                "payments_missing_reference": payments_missing_reference,
                "total_amount": float(total_amount),
            }
        )
    return {
        "rows": rows,
        "summary": {
            "missing_in_gateway": total_missing_gateway,
            "missing_in_dotmac": total_missing_dotmac,
            "payments_missing_reference": total_missing_references,
        },
    }


def list_data(db: Session, *, show_inactive: bool) -> dict[str, object]:
    """Build template context for the payment providers list page."""
    providers = billing_service.payment_providers.list(
        db=db,
        is_active=False if show_inactive else None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    providers = [
        item for item in providers if item.provider_type in SUPPORTED_PROVIDER_TYPES
    ]
    health_rows = _provider_health_rows(db)
    failover = get_failover_state(db)
    reconciliation = build_gateway_reconciliation(db)
    return {
        "providers": providers,
        "provider_types": supported_provider_type_values(),
        "show_inactive": show_inactive,
        "gateway_health": health_rows,
        "failover": failover,
        "gateway_reconciliation": reconciliation,
    }


def edit_data(db: Session, *, provider_id: str) -> dict[str, object] | None:
    """Build template context for the payment provider edit form."""
    provider = billing_service.payment_providers.get(db, provider_id)
    if not provider or provider.provider_type not in SUPPORTED_PROVIDER_TYPES:
        return None
    return {
        "provider": provider,
        "provider_types": supported_provider_type_values(),
    }


def create_provider_from_form(
    db: Session,
    *,
    name: str,
    provider_type: str,
    webhook_secret_ref: str | None,
    notes: str | None,
    is_active: str | None,
):
    payload = PaymentProviderCreate(
        name=name.strip(),
        provider_type=parse_supported_provider_type(provider_type),
        webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
        notes=notes.strip() if notes else None,
        is_active=is_active is not None,
    )
    return billing_service.payment_providers.create(db, payload)


def update_provider_from_form(
    db: Session,
    *,
    provider_id: UUID,
    name: str,
    provider_type: str,
    webhook_secret_ref: str | None,
    notes: str | None,
    is_active: str | None,
):
    payload = PaymentProviderUpdate(
        name=name.strip(),
        provider_type=parse_supported_provider_type(provider_type),
        webhook_secret_ref=webhook_secret_ref.strip() if webhook_secret_ref else None,
        notes=notes.strip() if notes else None,
        is_active=is_active is not None,
    )
    return billing_service.payment_providers.update(db, str(provider_id), payload)


def deactivate_provider(db: Session, *, provider_id: UUID) -> None:
    billing_service.payment_providers.delete(db, str(provider_id))
