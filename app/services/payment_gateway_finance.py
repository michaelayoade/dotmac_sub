"""Finance identities required by installation-backed payment gateways."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    PaymentChannel,
    PaymentChannelType,
    PaymentProvider,
    PaymentProviderType,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event


@dataclass(frozen=True, slots=True)
class GatewayFinanceIdentity:
    provider: PaymentProvider
    channel: PaymentChannel


class PaymentGatewayFinanceError(DomainError, ValueError):
    """Stable gateway finance-identity setup rejection."""


def _error(suffix: str, message: str) -> PaymentGatewayFinanceError:
    return PaymentGatewayFinanceError(
        code=f"financial.payment_gateway_finance.{suffix}",
        message=message,
    )


def ensure_gateway_identity(
    db: Session,
    *,
    provider_type: PaymentProviderType,
) -> GatewayFinanceIdentity:
    """Ensure the immutable provider/channel attribution pair for one gateway.

    This owner does not decide whether checkout is available. Connector
    installation state and capability bindings own that decision.
    """

    providers = list(
        db.scalars(
            select(PaymentProvider)
            .where(PaymentProvider.provider_type == provider_type)
            .order_by(PaymentProvider.created_at.asc(), PaymentProvider.id.asc())
        ).all()
    )
    if len(providers) > 1:
        raise _error(
            "provider_identity_ambiguous",
            f"{provider_type.value.title()} has multiple finance provider identities",
        )
    provider_created = False
    if providers:
        provider = providers[0]
    else:
        conflicting_name = db.scalar(
            select(PaymentProvider).where(
                PaymentProvider.name == provider_type.value.title()
            )
        )
        if conflicting_name is not None:
            raise _error(
                "provider_name_conflict",
                "Canonical gateway provider name is already assigned",
            )
        provider = PaymentProvider(
            name=provider_type.value.title(),
            provider_type=provider_type,
            is_active=True,
            notes="Canonical gateway settlement attribution identity",
        )
        db.add(provider)
        db.flush()
        provider_created = True

    channels = list(
        db.scalars(
            select(PaymentChannel)
            .where(PaymentChannel.provider_id == provider.id)
            .order_by(PaymentChannel.created_at.asc(), PaymentChannel.id.asc())
        ).all()
    )
    channel_created = False
    if len(channels) > 1:
        raise _error(
            "channel_identity_ambiguous",
            f"{provider_type.value.title()} has multiple settlement channels",
        )
    if channels:
        channel = channels[0]
    else:
        channel_name = f"{provider_type.value.title()} Online"
        conflicting_channel = db.scalar(
            select(PaymentChannel).where(PaymentChannel.name == channel_name)
        )
        if conflicting_channel is not None:
            raise _error(
                "channel_name_conflict",
                "Canonical gateway channel name is already assigned",
            )
        channel = PaymentChannel(
            name=channel_name,
            channel_type=PaymentChannelType.card,
            provider_id=provider.id,
            is_active=True,
            notes="Canonical online-gateway settlement channel",
        )
        db.add(channel)
        db.flush()
        channel_created = True
    if provider_created or channel_created:
        emit_event(
            db,
            EventType.payment_gateway_finance_identity_ensured,
            {
                "schema_version": 1,
                "provider_type": provider_type.value,
                "provider_id": str(provider.id),
                "channel_id": str(channel.id),
                "provider_created": provider_created,
                "channel_created": channel_created,
            },
        )
    return GatewayFinanceIdentity(provider=provider, channel=channel)


__all__ = [
    "GatewayFinanceIdentity",
    "PaymentGatewayFinanceError",
    "ensure_gateway_identity",
]
