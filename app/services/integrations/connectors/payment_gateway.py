"""DB-free Paystack and Flutterwave connector runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
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
            payload = {
                "authorization_code": str(params["authorization_code"]),
                "email": str(params["email"]),
                "amount": int(params["amount_kobo"]),
                "reference": str(params["reference"]),
            }
            if params.get("metadata"):
                payload["metadata"] = params["metadata"]
            return {
                "item": self._provider_data(
                    self._request(
                        "POST",
                        "/transaction/charge_authorization",
                        config=config,
                        secret_material=secret_material,
                        json_data=payload,
                    )
                )
            }
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
