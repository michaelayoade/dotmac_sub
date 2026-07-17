"""HTTP client for OpenAI-compatible LLM providers.

Pure transport: build a request, classify what came back, retry what is worth
retrying, and record telemetry. It makes no business decision and touches no
ORM row.

Ported near-verbatim from dotmac_crm. Dropped in the port: ``build_ai_client``
and ``_resolve_integration_ai_settings`` — they had no caller in CRM (only a
re-export), duplicated the gateway's config loading, and depended on an
``llm_provider`` setting the gateway does not use. ``AIGateway._client_for``
is the real construction path.
"""

from __future__ import annotations

import logging
import random
import socket
from dataclasses import dataclass
from time import perf_counter, sleep
from typing import Any

import httpx

from app.metrics import (
    observe_ai_provider_failure,
    observe_ai_provider_request,
    observe_ai_provider_retry_exhaustion,
)
from app.services.ai.security import ai_disabled_by_env, redact_secret_text

logger = logging.getLogger(__name__)


class AIClientError(RuntimeError):
    """Raised when AI generation fails."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        status_code: int | None = None,
        latency_ms: float | None = None,
        retry_count: int = 0,
        timeout_type: str | None = None,
        request_id: str | None = None,
        response_preview: str | None = None,
        failure_type: str = "unknown",
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.endpoint = endpoint
        self.status_code = status_code
        self.latency_ms = latency_ms
        self.retry_count = retry_count
        self.timeout_type = timeout_type
        self.request_id = request_id
        self.response_preview = response_preview
        self.failure_type = failure_type
        self.transient = transient


@dataclass(frozen=True)
class AIResponse:
    content: str
    tokens_in: int | None
    tokens_out: int | None
    model: str
    provider: str


def _coerce_int(value: object | None, default: int, minimum: int = 0) -> int:
    if value is None:
        parsed = default
    elif isinstance(value, bool | int | float):
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            parsed = default
    else:
        parsed = default
    return max(parsed, minimum)


def _coerce_float(value: object | None, default: float, minimum: float = 0.0) -> float:
    if value is None:
        parsed = default
    elif isinstance(value, bool | int | float):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            parsed = default
    else:
        parsed = default
    return max(parsed, minimum)


class _BaseHttpAIClient:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        temperature: float = 0.4,
    ) -> None:
        self.provider = provider
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature

    @staticmethod
    def _truncate_response_body(text: str | None, *, limit: int = 240) -> str | None:
        if not text:
            return None
        compact = " ".join(text.split())
        if len(compact) <= limit:
            return compact
        return f"{compact[:limit]}..."

    @staticmethod
    def _extract_request_id(response: httpx.Response | None) -> str | None:
        if response is None:
            return None
        return response.headers.get("x-request-id") or response.headers.get(
            "request-id"
        )

    @staticmethod
    def _timeout_type(exc: httpx.TimeoutException) -> str:
        if isinstance(exc, httpx.ReadTimeout):
            return "read"
        if isinstance(exc, httpx.ConnectTimeout):
            return "connect"
        if isinstance(exc, httpx.WriteTimeout):
            return "write"
        if isinstance(exc, httpx.PoolTimeout):
            return "pool"
        return "unknown"

    @staticmethod
    def _request_error_type(exc: httpx.RequestError) -> str:
        text = str(exc).lower()
        cause = exc.__cause__
        if (
            isinstance(cause, socket.gaierror)
            or "name or service not known" in text
            or "temporary failure in name resolution" in text
        ):
            return "dns_network"
        if "ssl" in text or "tls" in text or "certificate" in text:
            return "tls_handshake"
        if isinstance(exc, httpx.ConnectError):
            return "connection_error"
        return "network_error"

    def _classify_error(
        self,
        *,
        exc: Exception,
        endpoint: str,
        latency_ms: float,
        retry_count: int,
        response: httpx.Response | None = None,
    ) -> AIClientError:
        failure_type = "unknown"
        transient = False
        timeout_type = None
        status_code = response.status_code if response is not None else None
        request_id = self._extract_request_id(response)
        response_preview = redact_secret_text(
            self._truncate_response_body(
                response.text if response is not None else None
            )
        )

        if isinstance(exc, httpx.TimeoutException):
            failure_type = "timeout"
            timeout_type = self._timeout_type(exc)
            transient = True
        elif isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            request_id = self._extract_request_id(exc.response)
            response_preview = redact_secret_text(
                self._truncate_response_body(exc.response.text)
            )
            if status_code in {401, 403}:
                failure_type = "auth"
            elif status_code == 402 or (
                response_preview and "insufficient balance" in response_preview.lower()
            ):
                failure_type = "provider_billing"
            elif status_code == 429:
                failure_type = "rate_limit"
                transient = True
            elif status_code >= 500:
                failure_type = "provider_5xx"
                transient = True
            elif (
                status_code in {400, 404}
                and response_preview
                and "model" in response_preview.lower()
            ):
                failure_type = "model_unavailable"
            else:
                failure_type = "http_error"
        elif isinstance(exc, httpx.RequestError):
            failure_type = self._request_error_type(exc)
            transient = True
        elif isinstance(exc, ValueError):
            failure_type = "malformed_response"
        elif isinstance(exc, AIClientError):
            return exc

        parts = [
            "AI request failed",
            f"provider={self.provider}",
            f"model={self.model}",
            f"endpoint={endpoint}",
            f"failure_type={failure_type}",
        ]
        if status_code is not None:
            parts.append(f"http_status={status_code}")
        if timeout_type:
            parts.append(f"timeout_type={timeout_type}")
        if request_id:
            parts.append(f"request_id={request_id}")
        parts.append(f"latency_ms={latency_ms:.1f}")
        parts.append(f"retry_count={retry_count}")

        return AIClientError(
            " ".join(parts),
            provider=self.provider,
            model=self.model,
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
            retry_count=retry_count,
            timeout_type=timeout_type,
            request_id=request_id,
            response_preview=response_preview,
            failure_type=failure_type,
            transient=transient,
        )

    @staticmethod
    def _retry_backoff_seconds(attempt: int) -> float:
        # Jitter spreads retries so concurrent workers do not stampede a
        # recovering provider. Not security-sensitive.
        return min((2**attempt) + random.uniform(0.0, 0.5), 5.0)  # noqa: S311  # nosec B311

    @staticmethod
    def _log_failure(error: AIClientError, *, final: bool) -> None:
        level = logger.error if final else logger.warning
        parts = [
            "ai_provider_request_failed",
            f"provider={error.provider or 'unknown'}",
            f"model={error.model or 'unknown'}",
            f"endpoint={error.endpoint or 'unknown'}",
            f"failure_type={error.failure_type}",
        ]
        if error.status_code is not None:
            parts.append(f"http_status={error.status_code}")
        if error.timeout_type:
            parts.append(f"timeout_type={error.timeout_type}")
        if error.request_id:
            parts.append(f"request_id={error.request_id}")
        if error.latency_ms is not None:
            parts.append(f"latency_ms={error.latency_ms:.1f}")
        parts.append(f"retry_count={error.retry_count}")
        if error.response_preview:
            parts.append(
                f"response_preview={redact_secret_text(error.response_preview)!r}"
            )
        level(" ".join(parts))

    def _request_json(
        self, *, method: str, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if ai_disabled_by_env():
            raise AIClientError(
                "AI features are disabled (AI_ENABLED=false)",
                provider=self.provider,
                model=self.model,
                endpoint=url,
                failure_type="ai_disabled",
            )
        attempts = max(self.max_retries, 0) + 1
        for attempt in range(1, attempts + 1):
            start = perf_counter()
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.request(
                        method=method, url=url, headers=headers, json=payload
                    )
                latency_ms = (perf_counter() - start) * 1000.0
                if (
                    response.status_code in {408, 409, 425, 429}
                    or response.status_code >= 500
                ) and attempt < attempts:
                    error = self._classify_error(
                        exc=httpx.HTTPStatusError(
                            "retryable provider response",
                            request=response.request,
                            response=response,
                        ),
                        endpoint=url,
                        latency_ms=latency_ms,
                        retry_count=attempt - 1,
                        response=response,
                    )
                    observe_ai_provider_request(
                        provider=self.provider,
                        model=self.model,
                        endpoint=url,
                        outcome="failure",
                        latency_ms=latency_ms,
                    )
                    observe_ai_provider_failure(
                        provider=self.provider,
                        model=self.model,
                        endpoint=url,
                        failure_type=error.failure_type,
                    )
                    self._log_failure(error, final=False)
                    sleep(self._retry_backoff_seconds(attempt))
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict):
                    observe_ai_provider_request(
                        provider=self.provider,
                        model=self.model,
                        endpoint=url,
                        outcome="success",
                        latency_ms=latency_ms,
                    )
                    return data
                raise AIClientError(
                    "Invalid AI response payload",
                    provider=self.provider,
                    model=self.model,
                    endpoint=url,
                    latency_ms=latency_ms,
                    retry_count=attempt - 1,
                    failure_type="malformed_response",
                    transient=False,
                )
            except (
                httpx.TimeoutException,
                httpx.RequestError,
                httpx.HTTPStatusError,
                ValueError,
            ) as exc:
                latency_ms = (perf_counter() - start) * 1000.0
                error = self._classify_error(
                    exc=exc,
                    endpoint=url,
                    latency_ms=latency_ms,
                    retry_count=attempt - 1,
                    response=exc.response
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None,
                )
                observe_ai_provider_request(
                    provider=self.provider,
                    model=self.model,
                    endpoint=url,
                    outcome="failure",
                    latency_ms=latency_ms,
                )
                observe_ai_provider_failure(
                    provider=self.provider,
                    model=self.model,
                    endpoint=url,
                    failure_type=error.failure_type,
                )
                should_retry = error.transient and attempt < attempts
                self._log_failure(error, final=not should_retry)
                if should_retry:
                    sleep(self._retry_backoff_seconds(attempt))
                    continue
                if error.transient and error.retry_count >= self.max_retries:
                    observe_ai_provider_retry_exhaustion(
                        provider=self.provider,
                        model=self.model,
                        endpoint=url,
                        failure_type=error.failure_type,
                    )
                raise error from exc
            except AIClientError as exc:
                latency_ms = (perf_counter() - start) * 1000.0
                error = exc
                if error.latency_ms is None:
                    error.latency_ms = latency_ms
                observe_ai_provider_request(
                    provider=self.provider,
                    model=self.model,
                    endpoint=url,
                    outcome="failure",
                    latency_ms=latency_ms,
                )
                observe_ai_provider_failure(
                    provider=self.provider,
                    model=self.model,
                    endpoint=url,
                    failure_type=error.failure_type,
                )
                if error.transient and error.retry_count >= self.max_retries:
                    observe_ai_provider_retry_exhaustion(
                        provider=self.provider,
                        model=self.model,
                        endpoint=url,
                        failure_type=error.failure_type,
                    )
                self._log_failure(error, final=True)
                raise
        raise AIClientError(
            f"AI request failed for provider={self.provider}",
            provider=self.provider,
            model=self.model,
            endpoint=url,
            retry_count=max(attempts - 1, 0),
            failure_type="unknown",
        )


class VllmClient(_BaseHttpAIClient):
    """OpenAI-compatible client intended for vLLM (or any /v1/chat/completions endpoint).

    Note: despite the name, this works for hosted OpenAI-compatible providers
    (e.g. DeepSeek) and self-hosted gateways. The provider label is used for
    audit/debug only.
    """

    def __init__(
        self,
        *,
        provider: str = "vllm",
        api_key: str | None,
        model: str,
        base_url: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        temperature: float = 0.4,
    ) -> None:
        super().__init__(
            provider=provider,
            model=model,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            temperature=temperature,
        )
        self.api_key = (api_key or "").strip() or None
        self.base_url = base_url.rstrip("/")

    def _endpoint(self) -> str:
        # Allow base_url to be either ".../v1" or the root URL.
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def generate(self, system: str, prompt: str, max_tokens: int = 2048) -> AIResponse:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        data = self._request_json(
            method="POST",
            url=self._endpoint(),
            headers=headers,
            payload=payload,
        )

        usage = data.get("usage") if isinstance(data, dict) else {}
        if not isinstance(usage, dict):
            usage = {}

        content = ""
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    raw_content = message.get("content")
                    content = raw_content if isinstance(raw_content, str) else ""
                else:
                    raw_text = first.get("text")
                    content = raw_text if isinstance(raw_text, str) else ""

        return AIResponse(
            content=content.strip(),
            tokens_in=usage.get("prompt_tokens")
            if isinstance(usage.get("prompt_tokens"), int)
            else None,
            tokens_out=usage.get("completion_tokens")
            if isinstance(usage.get("completion_tokens"), int)
            else None,
            model=str(data.get("model") or self.model),
            provider=self.provider,
        )
