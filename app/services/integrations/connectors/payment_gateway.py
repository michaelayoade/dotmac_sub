"""DB-free Paystack and Flutterwave connector runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import JsonValue

from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)
from app.services.payment_charge_contracts import (
    AutopayChargeMetadata,
    ChargeAuthorizationCommand,
    ChargeAuthorizationObservation,
    ChargeMetadata,
    ChargeProtocolError,
    IntentChargeMetadata,
    InvoiceGatewayRoutingEvidence,
    ObserveAuthorizationChargeReference,
    PaymentGatewayProviderStatus,
    ProviderAcceptance,
    ResellerChargeMetadata,
)

PAYMENT_INTENT_CAPABILITY = "payments.intent.v1"
PAYMENT_WEBHOOK_CAPABILITY = "payments.webhook.v1"
PAYMENT_RECONCILE_CAPABILITY = "payments.reconcile.v1"
PAYMENT_REFUND_CAPABILITY = "payments.refund.v1"

_ACTIONS_BY_CAPABILITY = {
    PAYMENT_INTENT_CAPABILITY: {
        "get_public_key",
        "initialize",
        "charge_authorization",
    },
    PAYMENT_WEBHOOK_CAPABILITY: set(),
    PAYMENT_RECONCILE_CAPABILITY: {
        "verify",
        "fetch_refund",
        "list_refunds",
        "list_transactions",
    },
    PAYMENT_REFUND_CAPABILITY: {"refund"},
}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _subunits(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ChargeProtocolError("charge_money_invalid")
    return value


def _observed_currency(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise ChargeProtocolError("charge_currency_invalid")
    return value


def _provider_status(value: object) -> PaymentGatewayProviderStatus | None:
    if not isinstance(value, str):
        return None
    try:
        return PaymentGatewayProviderStatus(value.strip().lower())
    except ValueError:
        return None


def _paid_at(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChargeProtocolError("charge_timestamp_invalid")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChargeProtocolError("charge_timestamp_invalid") from exc
    if moment.tzinfo is None:
        raise ChargeProtocolError("charge_timestamp_invalid")
    return moment.astimezone(UTC)


def _charge_response(body: object) -> ChargeAuthorizationObservation:
    if not isinstance(body, Mapping):
        raise ChargeProtocolError("charge_response_invalid")
    flag = body.get("status")
    acceptance = (
        ProviderAcceptance.accepted
        if flag is True
        else ProviderAcceptance.rejected
        if flag is False
        else ProviderAcceptance.unknown
    )
    data = body.get("data")
    if not isinstance(data, Mapping):
        data = {}
    return ChargeAuthorizationObservation(
        acceptance=acceptance,
        provider_status=_provider_status(data.get("status")),
        reference=_text(data.get("reference")),
        gateway_response=_text(data.get("gateway_response")),
        message=_text(data.get("message")) or _text(body.get("message")),
        amount_subunits=_subunits(data.get("amount")),
        currency=_observed_currency(data.get("currency")),
        fee_subunits=_subunits(data.get("fees")),
        paid_at=_paid_at(
            data.get("paid_at")
            if data.get("paid_at") is not None
            else data.get("paidAt")
        ),
    )


def _charge_output(body: object, *, expected_reference: str) -> dict[str, JsonValue]:
    observation = _charge_response(body)
    if observation.acceptance is not ProviderAcceptance.accepted:
        raise ChargeProtocolError("charge_provider_not_accepted")
    if observation.provider_status is None:
        raise ChargeProtocolError("charge_provider_status_unknown")
    if not expected_reference or observation.reference != expected_reference:
        raise ChargeProtocolError("charge_reference_mismatch")
    # Persist only allowlisted facts, never provider authorization objects,
    # raw payloads, email or authorization-code material from the response.
    return {
        "charge_observation": {
            "acceptance": observation.acceptance.value,
            "provider_status": observation.provider_status.value
            if observation.provider_status
            else None,
            "reference": observation.reference,
            "gateway_response": observation.gateway_response,
            "message": observation.message,
            "amount_subunits": observation.amount_subunits,
            "currency": observation.currency,
            "fee_subunits": observation.fee_subunits,
            "paid_at": observation.paid_at.isoformat() if observation.paid_at else None,
        }
    }


def _charge_observation(
    output: object, *, expected_reference: str
) -> ChargeAuthorizationObservation:
    if not isinstance(output, Mapping) or not isinstance(
        output.get("charge_observation"), Mapping
    ):
        raise ChargeProtocolError("charge_observation_missing")
    value = output["charge_observation"]
    try:
        acceptance = ProviderAcceptance(value.get("acceptance"))
    except (TypeError, ValueError):
        acceptance = ProviderAcceptance.unknown
    observation = ChargeAuthorizationObservation(
        acceptance=acceptance,
        provider_status=_provider_status(value.get("provider_status")),
        reference=_text(value.get("reference")),
        gateway_response=_text(value.get("gateway_response")),
        message=_text(value.get("message")),
        amount_subunits=_subunits(value.get("amount_subunits")),
        currency=_observed_currency(value.get("currency")),
        fee_subunits=_subunits(value.get("fee_subunits")),
        paid_at=_paid_at(value.get("paid_at")),
    )
    if observation.acceptance is not ProviderAcceptance.accepted:
        raise ChargeProtocolError("charge_provider_not_accepted")
    if observation.provider_status is None:
        raise ChargeProtocolError("charge_provider_status_unknown")
    if not expected_reference or observation.reference != expected_reference:
        raise ChargeProtocolError("charge_reference_mismatch")
    return observation


def _charge_metadata(metadata: ChargeMetadata) -> dict[str, str | bool]:
    if isinstance(metadata, InvoiceGatewayRoutingEvidence):
        return dict(metadata.checkout_metadata())
    if isinstance(metadata, IntentChargeMetadata):
        return {"topup_intent_id": str(metadata.intent_id)}
    if isinstance(metadata, AutopayChargeMetadata):
        return {"invoice_id": str(metadata.invoice_id), "autopay": True}
    if isinstance(metadata, ResellerChargeMetadata):
        result: dict[str, str | bool] = {
            "payment_flow": "reseller_consolidated",
            "topup_intent_id": str(metadata.intent_id),
            "billing_account_id": str(metadata.billing_account_id),
            "reseller_id": str(metadata.reseller_id),
        }
        if metadata.payment_method_id is not None:
            result["payment_method_id"] = str(metadata.payment_method_id)
        return result
    raise ChargeProtocolError("charge_metadata_variant_invalid")


def execute_authorization_charge(
    command: ChargeAuthorizationCommand,
    execute: Callable[[str, dict[str, JsonValue]], OperationResult],
) -> ChargeAuthorizationObservation:
    params: dict[str, JsonValue] = {
        "authorization_code": command.authorization_code,
        "email": command.email,
        "amount_kobo": command.amount_subunits,
        "currency": command.currency,
        "reference": command.reference,
        "metadata": dict(_charge_metadata(command.metadata)),
    }
    result = execute("charge_authorization", params)
    if result.status is not OperationStatus.succeeded:
        raise ChargeProtocolError(result.error_code or "charge_transport_failed")
    return _charge_observation(result.output, expected_reference=command.reference)


def execute_authorization_observation(
    query: ObserveAuthorizationChargeReference,
    execute: Callable[[str, dict[str, JsonValue]], OperationResult],
) -> ChargeAuthorizationObservation:
    result = execute(
        "verify", {"reference": query.reference, "observe_authorization_charge": True}
    )
    if result.status is not OperationStatus.succeeded:
        raise ChargeProtocolError(result.error_code or "charge_observation_failed")
    return _charge_observation(result.output, expected_reference=query.reference)


def _required_charge_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ChargeProtocolError("charge_request_invalid")
    return value


def _authorization_payload(params: Mapping[str, object]) -> dict[str, JsonValue]:
    code = _required_charge_text(params.get("authorization_code"))
    email = _required_charge_text(params.get("email"))
    reference = _required_charge_text(params.get("reference"))
    currency = _required_charge_text(params.get("currency"))
    amount = params.get("amount_kobo")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ChargeProtocolError("charge_amount_invalid")
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or currency != currency.upper()
    ):
        raise ChargeProtocolError("charge_currency_invalid")
    metadata = params.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ChargeProtocolError("charge_metadata_invalid")
    serialized: dict[str, JsonValue] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, (str, bool)):
            raise ChargeProtocolError("charge_metadata_invalid")
        serialized[key] = value
    return {
        "authorization_code": code,
        "email": email,
        "reference": reference,
        "currency": currency,
        "amount": amount,
        "metadata": serialized,
    }


class PaymentGatewayRunner:
    """Run an allow-listed provider operation with materialized credentials."""

    def __init__(
        self, provider: str, client_override: httpx.Client | None = None
    ) -> None:
        if provider not in {"paystack", "flutterwave"}:
            raise ValueError("unsupported payment provider")
        self.provider = provider
        self._client_override = client_override

    def _base_url(self, config: Mapping[str, Any]) -> str:
        return str(config.get("base_url") or "").rstrip("/")

    def _timeout(self, config: Mapping[str, Any]) -> float:
        return float(config.get("timeout_seconds") or 30)

    def _headers(self, secret_material: Mapping[str, str]) -> dict[str, str]:
        credential = str(secret_material.get("gateway_credentials") or "")
        return {"Authorization": f"Bearer {credential}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._client_override or httpx.Client()
        owned = self._client_override is None
        try:
            response = client.request(
                method,
                f"{self._base_url(config)}{path}",
                headers=self._headers(secret_material),
                json=json_data,
                params=params,
                timeout=self._timeout(config),
            )
            response.raise_for_status()
            body = response.json()
        finally:
            if owned:
                client.close()
        if not isinstance(body, dict):
            raise ValueError("provider response is not an object")
        return body

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        if not self._base_url(config):
            return ValidationResult(valid=False, error_codes=("base_url_missing",))
        if not secret_material.get("gateway_credentials"):
            return ValidationResult(
                valid=False, error_codes=("gateway_credentials_missing",)
            )
        path = "/bank" if self.provider == "paystack" else "/banks/NG"
        try:
            self._request(
                "GET",
                path,
                config=config,
                secret_material=secret_material,
                params={"perPage": 1},
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            return ValidationResult(valid=False, error_codes=(f"provider_http_{code}",))
        except Exception:
            return ValidationResult(valid=False, error_codes=("provider_unreachable",))
        return ValidationResult(valid=True)

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        allowed = _ACTIONS_BY_CAPABILITY.get(envelope.capability_id)
        action = str(envelope.payload.get("action") or "")
        params = envelope.payload.get("params") or {}
        if allowed is None or action not in allowed or not isinstance(params, dict):
            return self._result(
                envelope, OperationStatus.rejected, "operation_not_allowed"
            )
        try:
            output = self._execute_action(action, params, config, secret_material)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            status = (
                OperationStatus.retryable
                if status_code >= 500
                else OperationStatus.rejected
            )
            return self._result(envelope, status, f"provider_http_{status_code}")
        except httpx.HTTPError:
            return self._result(
                envelope, OperationStatus.retryable, "provider_transport_error"
            )
        except ChargeProtocolError as exc:
            return self._result(envelope, OperationStatus.rejected, exc.message)
        except (KeyError, TypeError, ValueError):
            return self._result(
                envelope, OperationStatus.rejected, "provider_operation_invalid"
            )
        except Exception:
            return self._result(
                envelope, OperationStatus.failed, "provider_connector_failed"
            )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output=output,
        )

    @staticmethod
    def _result(
        envelope: OperationEnvelope, status: OperationStatus, error_code: str
    ) -> OperationResult:
        return OperationResult(
            operation_id=envelope.operation_id, status=status, error_code=error_code
        )

    def _provider_data(self, body: dict[str, Any]) -> dict[str, Any]:
        success = (
            body.get("status") is True
            if self.provider == "paystack"
            else body.get("status") == "success"
        )
        if not success:
            raise ValueError(str(body.get("message") or "provider operation failed"))
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("provider data is invalid")
        return data

    def _execute_action(
        self,
        action: str,
        params: dict[str, Any],
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> dict[str, Any]:
        if action == "get_public_key":
            return {"value": str(secret_material.get("public_key") or "")}
        if action == "initialize":
            return {"item": self._initialize(params, config, secret_material)}
        if action == "charge_authorization":
            if self.provider != "paystack":
                raise ValueError("saved authorization charging is unsupported")
            return _charge_output(
                self._request(
                    "POST",
                    "/transaction/charge_authorization",
                    config=config,
                    secret_material=secret_material,
                    json_data=_authorization_payload(params),
                ),
                expected_reference=_required_charge_text(params.get("reference")),
            )
        if action == "verify":
            reference = quote(str(params["reference"]), safe="")
            if self.provider == "paystack":
                body = self._request(
                    "GET",
                    f"/transaction/verify/{reference}",
                    config=config,
                    secret_material=secret_material,
                )
            else:
                body = self._request(
                    "GET",
                    "/transactions/verify_by_reference",
                    config=config,
                    secret_material=secret_material,
                    params={"tx_ref": str(params["reference"])},
                )
            if params.get("observe_authorization_charge") is True:
                if self.provider != "paystack":
                    raise ChargeProtocolError("charge_observation_provider_invalid")
                return _charge_output(
                    body,
                    expected_reference=_required_charge_text(params.get("reference")),
                )
            return {"item": self._provider_data(body)}
        if action == "refund":
            return {"item": self._refund(params, config, secret_material)}
        if action == "fetch_refund":
            path = (
                f"/refund/{quote(str(params['refund_id']), safe='')}"
                if self.provider == "paystack"
                else f"/refunds/{quote(str(params['refund_id']), safe='')}"
            )
            return {
                "item": self._provider_data(
                    self._request(
                        "GET", path, config=config, secret_material=secret_material
                    )
                )
            }
        if action == "list_refunds":
            query = (
                {"transaction": str(params["transaction_id"]), "perPage": 100}
                if self.provider == "paystack"
                else {"id": str(params["transaction_id"])}
            )
            body = self._request(
                "GET",
                "/refund" if self.provider == "paystack" else "/refunds",
                config=config,
                secret_material=secret_material,
                params=query,
            )
            success = (
                body.get("status") is True
                if self.provider == "paystack"
                else body.get("status") == "success"
            )
            if not success:
                raise ValueError(str(body.get("message") or "refund lookup failed"))
            return {
                "items": [
                    dict(item)
                    for item in body.get("data") or []
                    if isinstance(item, dict)
                ]
            }
        if action == "list_transactions":
            if self.provider != "paystack":
                raise ValueError("transaction listing is unsupported")
            body = self._request(
                "GET",
                "/transaction",
                config=config,
                secret_material=secret_material,
                params={
                    "from": str(params["from_date"]),
                    "to": str(params["to_date"]),
                    "page": int(params["page"]),
                    "perPage": int(params["per_page"]),
                    **(
                        {"status": str(params["status"])}
                        if params.get("status")
                        else {}
                    ),
                },
            )
            if body.get("status") is not True:
                raise ValueError(str(body.get("message") or "transaction list failed"))
            return {
                "items": [
                    dict(item)
                    for item in body.get("data") or []
                    if isinstance(item, dict)
                ],
                "meta": dict(body.get("meta") or {})
                if isinstance(body.get("meta"), dict)
                else {},
            }
        raise ValueError("unsupported payment action")

    def _initialize(
        self,
        params: dict[str, Any],
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> dict[str, Any]:
        if self.provider == "paystack":
            payload: dict[str, Any] = {
                "email": str(params["email"]),
                "amount": int(params["amount_kobo"]),
                "reference": str(params["reference"]),
                "callback_url": str(params["redirect_url"]),
            }
            path = "/transaction/initialize"
        else:
            payload = {
                "tx_ref": str(params["reference"]),
                "amount": float(Decimal(str(params["amount"]))),
                "currency": str(
                    params.get("currency") or config.get("default_currency") or "NGN"
                ).upper(),
                "redirect_url": str(params["redirect_url"]),
                "customer": {"email": str(params["email"])},
            }
            path = "/payments"
        if params.get("metadata"):
            payload["metadata" if self.provider == "paystack" else "meta"] = params[
                "metadata"
            ]
        return self._provider_data(
            self._request(
                "POST",
                path,
                config=config,
                secret_material=secret_material,
                json_data=payload,
            )
        )

    def _refund(
        self,
        params: dict[str, Any],
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> dict[str, Any]:
        amount = params.get("amount")
        request_key = str(params.get("request_key") or "")
        if self.provider == "paystack":
            payload: dict[str, Any] = {"transaction": str(params["transaction_id"])}
            if amount is not None:
                payload["amount"] = int(Decimal(str(amount)) * 100)
            if request_key:
                payload["merchant_note"] = request_key
            path = "/refund"
        else:
            payload = {}
            if amount is not None:
                payload["amount"] = float(Decimal(str(amount)))
            if request_key:
                payload["comments"] = request_key
            path = (
                f"/transactions/{quote(str(params['transaction_id']), safe='')}/refund"
            )
        return self._provider_data(
            self._request(
                "POST",
                path,
                config=config,
                secret_material=secret_material,
                json_data=payload,
            )
        )

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> HealthResult:
        validation = self.validate(
            manifest=manifest, config=config, secret_material=secret_material
        )
        return HealthResult(
            status="healthy" if validation.valid else "unavailable",
            details={"error_codes": list(validation.error_codes)},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False
