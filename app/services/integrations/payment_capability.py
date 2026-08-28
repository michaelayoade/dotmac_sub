"""Capability facade for payment-provider transports and inbound authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationInstallationState,
)
from app.services.common import coerce_uuid
from app.services.integrations import installations
from app.services.integrations.connectors.payment_gateway import (
    PAYMENT_INTENT_CAPABILITY,
    PAYMENT_RECONCILE_CAPABILITY,
    PAYMENT_REFUND_CAPABILITY,
    PAYMENT_WEBHOOK_CAPABILITY,
)
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    build_execution_context,
    make_operation_executor,
)
from app.services.secrets import resolve_secret

logger = logging.getLogger(__name__)

WEBHOOK_INGRESS_POLICY_KEY = "ingress_rate_limit"
WEBHOOK_INGRESS_LIMIT_MIN = 10
WEBHOOK_INGRESS_LIMIT_MAX = 1000
WEBHOOK_INGRESS_WINDOW_MIN = 10
WEBHOOK_INGRESS_WINDOW_MAX = 300
_WEBHOOK_INGRESS_CACHE_PREFIX = "integration:payment-webhook-ingress-policy"


@dataclass(frozen=True, slots=True)
class WebhookIngressPolicy:
    """Bounded pre-database traffic policy for one signed provider ingress."""

    requests_per_window: int
    window_seconds: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if (
            not WEBHOOK_INGRESS_LIMIT_MIN
            <= self.requests_per_window
            <= WEBHOOK_INGRESS_LIMIT_MAX
        ):
            raise PaymentCapabilityError(
                "webhook ingress requests must be between "
                f"{WEBHOOK_INGRESS_LIMIT_MIN} and {WEBHOOK_INGRESS_LIMIT_MAX}"
            )
        if (
            not WEBHOOK_INGRESS_WINDOW_MIN
            <= self.window_seconds
            <= WEBHOOK_INGRESS_WINDOW_MAX
        ):
            raise PaymentCapabilityError(
                "webhook ingress window must be between "
                f"{WEBHOOK_INGRESS_WINDOW_MIN} and {WEBHOOK_INGRESS_WINDOW_MAX} seconds"
            )

    def binding_fragment(self) -> dict[str, int]:
        return {
            "requests_per_window": self.requests_per_window,
            "window_seconds": self.window_seconds,
        }


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return min(max(int(raw), minimum), maximum)
    except ValueError:
        logger.warning("Invalid integer env %s; using %s", name, default)
        return default


def default_webhook_ingress_policy() -> WebhookIngressPolicy:
    return WebhookIngressPolicy(
        requests_per_window=_bounded_env_int(
            "PAYMENT_WEBHOOK_RATE_LIMIT_PER_IP",
            120,
            WEBHOOK_INGRESS_LIMIT_MIN,
            WEBHOOK_INGRESS_LIMIT_MAX,
        ),
        window_seconds=_bounded_env_int(
            "PAYMENT_WEBHOOK_RATE_LIMIT_WINDOW_SECONDS",
            60,
            WEBHOOK_INGRESS_WINDOW_MIN,
            WEBHOOK_INGRESS_WINDOW_MAX,
        ),
    )


def webhook_ingress_policy_from_binding(
    policy_json: Mapping[str, object] | None,
) -> WebhookIngressPolicy:
    raw = (policy_json or {}).get(WEBHOOK_INGRESS_POLICY_KEY)
    if not isinstance(raw, Mapping):
        return default_webhook_ingress_policy()
    default = default_webhook_ingress_policy()
    try:
        requests_per_window = int(
            raw.get("requests_per_window", default.requests_per_window)
        )
        window_seconds = int(raw.get("window_seconds", default.window_seconds))
    except (TypeError, ValueError) as exc:
        raise PaymentCapabilityError("webhook ingress policy is invalid") from exc
    return WebhookIngressPolicy(
        requests_per_window=requests_per_window,
        window_seconds=window_seconds,
    )


def binding_policy_with_webhook_ingress(
    existing: Mapping[str, object] | None,
    ingress: WebhookIngressPolicy,
) -> dict[str, object]:
    policy = dict(existing or {})
    policy[WEBHOOK_INGRESS_POLICY_KEY] = ingress.binding_fragment()
    return policy


def publish_webhook_ingress_policy(
    provider_type: str, policy: WebhookIngressPolicy
) -> bool:
    from app.services.redis_client import safe_set

    provider = _connector(provider_type)
    return safe_set(
        f"{_WEBHOOK_INGRESS_CACHE_PREFIX}:{provider}",
        json.dumps(policy.binding_fragment(), sort_keys=True),
    )


def effective_webhook_ingress_policy(provider_type: str) -> WebhookIngressPolicy:
    if os.getenv("PAYMENT_WEBHOOK_RATE_LIMIT_ENABLED", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        default = default_webhook_ingress_policy()
        return WebhookIngressPolicy(
            requests_per_window=default.requests_per_window,
            window_seconds=default.window_seconds,
            enabled=False,
        )

    from app.services.redis_client import safe_get

    provider = _connector(provider_type)
    raw = safe_get(f"{_WEBHOOK_INGRESS_CACHE_PREFIX}:{provider}")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return webhook_ingress_policy_from_binding(
                    {WEBHOOK_INGRESS_POLICY_KEY: decoded}
                )
        except (json.JSONDecodeError, PaymentCapabilityError, UnicodeDecodeError):
            logger.warning("Invalid cached webhook ingress policy for %s", provider)
    return default_webhook_ingress_policy()


def hydrate_webhook_ingress_policies(db: Session) -> int:
    """Rebuild runtime policy projections from enabled binding authority."""

    rows = (
        db.query(IntegrationCapabilityBinding)
        .filter(
            IntegrationCapabilityBinding.capability_id == PAYMENT_WEBHOOK_CAPABILITY,
            IntegrationCapabilityBinding.state == IntegrationBindingState.enabled.value,
        )
        .all()
    )
    published = 0
    for binding in rows:
        if binding.installation.state != IntegrationInstallationState.enabled.value:
            continue
        policy = webhook_ingress_policy_from_binding(binding.policy_json)
        publish_webhook_ingress_policy(binding.installation.connector_key, policy)
        published += 1
    return published


class PaymentCapabilityError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error_code = message
        self.status_code: int | None = None
        if message.startswith("provider_http_"):
            try:
                self.status_code = int(message.rsplit("_", 1)[1])
            except ValueError:
                pass


def is_verification_not_found(error: PaymentCapabilityError) -> bool:
    """Classify provider evidence that proves no charge exists for a reference."""

    return error.status_code in {400, 404}


def _connector(provider_type: str) -> str:
    provider = provider_type.strip().lower()
    if provider not in {"paystack", "flutterwave"}:
        raise PaymentCapabilityError(f"unsupported payment provider {provider_type!r}")
    return provider


def _binding(
    db: Session,
    provider_type: str,
    capability_id: str,
    *,
    checkout_binding_id: str | uuid.UUID | None = None,
):
    if checkout_binding_id is not None:
        source = db.get(
            IntegrationCapabilityBinding,
            coerce_uuid(str(checkout_binding_id)),
        )
        if source is None:
            raise PaymentCapabilityError("checkout capability binding not found")
        if source.capability_id != PAYMENT_INTENT_CAPABILITY:
            raise PaymentCapabilityError(
                "checkout capability binding is not a payment intent binding"
            )
        installation = source.installation
        if installation.connector_key != _connector(provider_type):
            raise PaymentCapabilityError(
                "checkout capability binding provider mismatch"
            )
        sibling = (
            db.query(IntegrationCapabilityBinding)
            .filter(
                IntegrationCapabilityBinding.installation_id == installation.id,
                IntegrationCapabilityBinding.capability_id == capability_id,
                IntegrationCapabilityBinding.state
                == IntegrationBindingState.enabled.value,
            )
            .one_or_none()
        )
        if (
            sibling is None
            or installation.state != IntegrationInstallationState.enabled.value
        ):
            raise PaymentCapabilityError(
                f"pinned installation has no enabled binding for {capability_id}"
            )
        return sibling
    return installations.require_enabled_capability_binding(
        db, connector_key=_connector(provider_type), capability_id=capability_id
    )


def _execute(
    db: Session,
    *,
    provider_type: str,
    capability_id: str,
    action: str,
    params: dict[str, Any],
    trigger: OperationTrigger,
    correlation_id: str,
    checkout_binding_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    binding = _binding(
        db,
        provider_type,
        capability_id,
        checkout_binding_id=checkout_binding_id,
    )
    context = build_execution_context(db, capability_binding_id=binding.id)
    result = make_operation_executor(
        context,
        correlation_id=correlation_id[:160],
        trigger=trigger,
        actor="integration.payments",
    )(action, params)
    if result.status != OperationStatus.succeeded:
        raise PaymentCapabilityError(result.error_code or "payment capability failed")
    return dict(result.output)


def generate_reference(invoice_number: str | None = None) -> str:
    suffix = uuid.uuid4().hex[:8]
    return f"DMAC-{invoice_number}-{suffix}" if invoice_number else f"DMAC-{suffix}"


def amount_to_kobo(amount: Decimal | float | int) -> int:
    return int(Decimal(str(amount)) * 100)


def kobo_to_naira(kobo: int | str | Decimal) -> Decimal:
    return Decimal(str(kobo)) / 100


def get_public_key(
    db: Session,
    provider_type: str,
    *,
    checkout_binding_id: str | uuid.UUID | None = None,
) -> str:
    return str(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_INTENT_CAPABILITY,
            action="get_public_key",
            params={},
            trigger=OperationTrigger.interactive,
            correlation_id=f"payment-public-key:{provider_type}",
            checkout_binding_id=checkout_binding_id,
        ).get("value")
        or ""
    )


def initialize_transaction(
    db: Session,
    *,
    provider_type: str,
    email: str,
    reference: str,
    redirect_url: str,
    amount: Decimal | float | int | None = None,
    amount_kobo: int | None = None,
    metadata: dict[str, Any] | None = None,
    currency: str | None = None,
    checkout_binding_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_INTENT_CAPABILITY,
            action="initialize",
            params={
                "email": email,
                "reference": reference,
                "redirect_url": redirect_url,
                "amount": amount,
                "amount_kobo": amount_kobo,
                "metadata": metadata or {},
                "currency": currency,
            },
            trigger=OperationTrigger.interactive,
            correlation_id=f"payment-initialize:{provider_type}:{reference}",
            checkout_binding_id=checkout_binding_id,
        ).get("item")
        or {}
    )


def charge_authorization(
    db: Session,
    *,
    authorization_code: str,
    email: str,
    amount_kobo: int,
    reference: str,
    metadata: dict[str, Any] | None = None,
    checkout_binding_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type="paystack",
            capability_id=PAYMENT_INTENT_CAPABILITY,
            action="charge_authorization",
            params={
                "authorization_code": authorization_code,
                "email": email,
                "amount_kobo": amount_kobo,
                "reference": reference,
                "metadata": metadata or {},
            },
            trigger=OperationTrigger.event,
            correlation_id=f"payment-charge-authorization:{reference}",
            checkout_binding_id=checkout_binding_id,
        ).get("item")
        or {}
    )


def verify_transaction(
    db: Session,
    *,
    provider_type: str,
    reference: str,
    checkout_binding_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
            action="verify",
            params={"reference": reference},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"payment-verify:{provider_type}:{reference}",
            checkout_binding_id=checkout_binding_id,
        ).get("item")
        or {}
    )


def refund_transaction(
    db: Session,
    *,
    provider_type: str,
    transaction_id: str,
    amount: Decimal | None = None,
    request_key: str | None = None,
) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_REFUND_CAPABILITY,
            action="refund",
            params={
                "transaction_id": transaction_id,
                "amount": str(amount) if amount is not None else None,
                "request_key": request_key,
            },
            trigger=OperationTrigger.event,
            correlation_id=f"payment-refund:{provider_type}:{request_key or transaction_id}",
        ).get("item")
        or {}
    )


def fetch_refund(db: Session, *, provider_type: str, refund_id: str) -> dict[str, Any]:
    return dict(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
            action="fetch_refund",
            params={"refund_id": refund_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"payment-refund-read:{provider_type}:{refund_id}",
        ).get("item")
        or {}
    )


def list_refunds(
    db: Session, *, provider_type: str, transaction_id: str
) -> list[dict[str, Any]]:
    return list(
        _execute(
            db,
            provider_type=provider_type,
            capability_id=PAYMENT_RECONCILE_CAPABILITY,
            action="list_refunds",
            params={"transaction_id": transaction_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"payment-refunds:{provider_type}:{transaction_id}",
        ).get("items")
        or []
    )


def list_transactions_page(
    db: Session,
    *,
    provider_type: str,
    from_date: str,
    to_date: str,
    status: str | None,
    page: int,
    per_page: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output = _execute(
        db,
        provider_type=provider_type,
        capability_id=PAYMENT_RECONCILE_CAPABILITY,
        action="list_transactions",
        params={
            "from_date": from_date,
            "to_date": to_date,
            "status": status,
            "page": page,
            "per_page": per_page,
        },
        trigger=OperationTrigger.reconcile,
        correlation_id=(
            f"payment-transactions:{provider_type}:{from_date}:{to_date}:{page}"
        ),
    )
    return list(output.get("items") or []), dict(output.get("meta") or {})


def inbound_context(
    db: Session,
    *,
    provider_type: str,
    secret_resolver: Callable[[str | None], str | None] = resolve_secret,
):
    binding = _binding(db, provider_type, PAYMENT_WEBHOOK_CAPABILITY)
    context = build_execution_context(
        db, capability_binding_id=binding.id, secret_resolver=secret_resolver
    )
    return binding, dict(context.secret_material)


def verify_webhook_signature(
    db: Session, *, provider_type: str, body: bytes, signature: str
) -> tuple[Any, bool]:
    binding, material = inbound_context(db, provider_type=provider_type)
    if provider_type == "paystack":
        secret = str(material.get("gateway_credentials") or "")
        expected = (
            hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
            if secret
            else ""
        )
    else:
        expected = str(material.get("webhook_signing_secret") or "")
    return binding, bool(
        expected and signature and hmac.compare_digest(expected, signature)
    )
