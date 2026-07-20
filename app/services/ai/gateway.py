"""The AI provider boundary: config, circuit breaker, and fallback.

``docs/designs/AI_SOT.md`` declares ``ai.gateway`` a **transport, not a
decision system** — the same species as a payment gateway. It owns no domain
state and makes no business decision; the only judgement it exercises is
whether a provider is currently worth calling.

Ported near-verbatim from dotmac_crm. Divergences:

* **Credentials come from the stored setting, resolved through OpenBao**
  (``ai.security.resolve_provider_api_key``), never from the environment —
  Sub's ``SettingSpec`` contract reserves ``env_var`` for bootstrap, not
  runtime override. The ``base_url``/``env_var`` arguments CRM passed for its
  DeepSeek env special-case are therefore gone.
* Circuit-breaker state is per-process and in-memory, exactly as in CRM. See
  ``app/metrics.py`` for why that matters to what ``/metrics`` can show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.metrics import (
    observe_ai_provider_fallback,
    set_ai_provider_circuit_open,
    set_ai_provider_circuit_open_duration,
)
from app.models.domain_settings import SettingDomain
from app.services.ai.client import (
    AIClientError,
    AIResponse,
    VllmClient,
    _coerce_float,
    _coerce_int,
)
from app.services.ai.security import (
    ai_enabled,
    redact_secret_text,
    resolve_provider_api_key,
)
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

AIEndpoint = Literal["primary", "secondary"]
# Per-process transport resilience, not a business decision — so these are
# constants, not env reads (this repo forbids direct env decision inputs) and
# not settings (a DB read at import time). Promote to settings only if an
# operator ever needs to disagree at runtime.
_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 60


@dataclass(frozen=True)
class AIEndpointConfig:
    label: str
    base_url: str
    model: str
    api_key: str | None
    require_api_key: bool
    timeout_seconds: float
    max_retries: int
    max_tokens: int


@dataclass
class _CircuitBreakerState:
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    opened_at: datetime | None = None


def _get_bool(
    db: Session, domain: SettingDomain, key: str, default: bool = False
) -> bool:
    value = resolve_value(db, domain, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _load_primary_config(db: Session) -> AIEndpointConfig:
    label = (
        str(
            resolve_value(db, SettingDomain.integration, "vllm_label") or "primary"
        ).strip()
        or "primary"
    )
    base_url = str(
        resolve_value(db, SettingDomain.integration, "vllm_base_url") or ""
    ).strip()
    model = str(
        resolve_value(db, SettingDomain.integration, "vllm_model") or ""
    ).strip()
    api_key = resolve_provider_api_key(
        configured_api_key=resolve_value(db, SettingDomain.integration, "vllm_api_key"),
    )
    require_api_key = _get_bool(
        db, SettingDomain.integration, "vllm_require_api_key", default=False
    )
    timeout_seconds = _coerce_float(
        resolve_value(db, SettingDomain.integration, "vllm_timeout_seconds"),
        default=30.0,
        minimum=1.0,
    )
    max_retries = _coerce_int(
        resolve_value(db, SettingDomain.integration, "vllm_max_retries"),
        default=2,
        minimum=0,
    )
    max_tokens = _coerce_int(
        resolve_value(db, SettingDomain.integration, "vllm_max_tokens"),
        default=2048,
        minimum=1,
    )
    return AIEndpointConfig(
        label=label,
        base_url=base_url,
        model=model,
        api_key=api_key,
        require_api_key=require_api_key,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_tokens=max_tokens,
    )


def _load_secondary_config(db: Session) -> AIEndpointConfig:
    label = (
        str(
            resolve_value(db, SettingDomain.integration, "vllm_secondary_label")
            or "secondary"
        ).strip()
        or "secondary"
    )
    base_url = str(
        resolve_value(db, SettingDomain.integration, "vllm_secondary_base_url") or ""
    ).strip()
    model = str(
        resolve_value(db, SettingDomain.integration, "vllm_secondary_model") or ""
    ).strip()
    api_key = resolve_provider_api_key(
        configured_api_key=resolve_value(
            db, SettingDomain.integration, "vllm_secondary_api_key"
        ),
    )
    require_api_key = _get_bool(
        db, SettingDomain.integration, "vllm_secondary_require_api_key", default=False
    )
    timeout_seconds = _coerce_float(
        resolve_value(db, SettingDomain.integration, "vllm_secondary_timeout_seconds"),
        default=30.0,
        minimum=1.0,
    )
    max_retries = _coerce_int(
        resolve_value(db, SettingDomain.integration, "vllm_secondary_max_retries"),
        default=1,
        minimum=0,
    )
    max_tokens = _coerce_int(
        resolve_value(db, SettingDomain.integration, "vllm_secondary_max_tokens"),
        default=2048,
        minimum=1,
    )
    return AIEndpointConfig(
        label=label,
        base_url=base_url,
        model=model,
        api_key=api_key,
        require_api_key=require_api_key,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_tokens=max_tokens,
    )


class AIGateway:
    """
    Central place for AI calls.

    - Keeps all provider settings + retries + max token policy in one place.
    - Supports two endpoints (primary + secondary) so you can combine a hosted
      provider with a self-hosted model.
    """

    def __init__(self) -> None:
        self._circuit_states: dict[str, _CircuitBreakerState] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _circuit_key(self, cfg: AIEndpointConfig, endpoint: AIEndpoint) -> str:
        return f"{endpoint}:{cfg.label}:{cfg.model}:{cfg.base_url}"

    def _get_state(
        self, cfg: AIEndpointConfig, endpoint: AIEndpoint
    ) -> _CircuitBreakerState:
        key = self._circuit_key(cfg, endpoint)
        return self._circuit_states.setdefault(key, _CircuitBreakerState())

    def _before_request(self, cfg: AIEndpointConfig, endpoint: AIEndpoint) -> None:
        state = self._get_state(cfg, endpoint)
        now = self._now()
        if state.cooldown_until and now < state.cooldown_until:
            set_ai_provider_circuit_open(
                provider=cfg.label, model=cfg.model, endpoint=endpoint, is_open=True
            )
            raise AIClientError(
                f"AI circuit open provider={cfg.label} endpoint={endpoint} "
                f"until={state.cooldown_until.isoformat()}",
                provider=cfg.label,
                model=cfg.model,
                endpoint=endpoint,
                failure_type="circuit_open",
                transient=True,
            )
        if state.cooldown_until and now >= state.cooldown_until:
            open_duration_seconds = 0.0
            if state.opened_at is not None:
                open_duration_seconds = max(
                    (now - state.opened_at).total_seconds(), 0.0
                )
            state.cooldown_until = None
            state.consecutive_failures = 0
            state.opened_at = None
            set_ai_provider_circuit_open(
                provider=cfg.label, model=cfg.model, endpoint=endpoint, is_open=False
            )
            set_ai_provider_circuit_open_duration(
                provider=cfg.label,
                model=cfg.model,
                endpoint=endpoint,
                duration_seconds=0.0,
            )
            logger.info(
                "ai_provider_circuit_recovered provider=%s model=%s endpoint=%s "
                "previous_open_duration_seconds=%.1f",
                cfg.label,
                cfg.model,
                endpoint,
                open_duration_seconds,
            )

    def _record_success(self, cfg: AIEndpointConfig, endpoint: AIEndpoint) -> None:
        state = self._get_state(cfg, endpoint)
        state.consecutive_failures = 0
        state.cooldown_until = None
        state.opened_at = None
        set_ai_provider_circuit_open(
            provider=cfg.label, model=cfg.model, endpoint=endpoint, is_open=False
        )
        set_ai_provider_circuit_open_duration(
            provider=cfg.label,
            model=cfg.model,
            endpoint=endpoint,
            duration_seconds=0.0,
        )

    def _record_failure(
        self, cfg: AIEndpointConfig, endpoint: AIEndpoint, error: AIClientError
    ) -> None:
        state = self._get_state(cfg, endpoint)
        if not error.transient:
            state.consecutive_failures = 0
            state.cooldown_until = None
            state.opened_at = None
            set_ai_provider_circuit_open(
                provider=cfg.label, model=cfg.model, endpoint=endpoint, is_open=False
            )
            set_ai_provider_circuit_open_duration(
                provider=cfg.label,
                model=cfg.model,
                endpoint=endpoint,
                duration_seconds=0.0,
            )
            return
        state.consecutive_failures += 1
        if state.consecutive_failures < _CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            set_ai_provider_circuit_open(
                provider=cfg.label, model=cfg.model, endpoint=endpoint, is_open=False
            )
            set_ai_provider_circuit_open_duration(
                provider=cfg.label,
                model=cfg.model,
                endpoint=endpoint,
                duration_seconds=0.0,
            )
            return
        state.opened_at = state.opened_at or self._now()
        state.cooldown_until = self._now() + timedelta(
            seconds=_CIRCUIT_BREAKER_COOLDOWN_SECONDS
        )
        set_ai_provider_circuit_open(
            provider=cfg.label, model=cfg.model, endpoint=endpoint, is_open=True
        )
        set_ai_provider_circuit_open_duration(
            provider=cfg.label,
            model=cfg.model,
            endpoint=endpoint,
            duration_seconds=max((self._now() - state.opened_at).total_seconds(), 0.0),
        )
        logger.warning(
            "ai_provider_circuit_opened provider=%s model=%s endpoint=%s failure_type=%s "
            "consecutive_failures=%s cooldown_seconds=%s",
            cfg.label,
            cfg.model,
            endpoint,
            error.failure_type,
            state.consecutive_failures,
            _CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )

    def enabled(self, db: Session) -> bool:
        return ai_enabled(db)

    def get_endpoint_config(
        self, db: Session, endpoint: AIEndpoint
    ) -> AIEndpointConfig:
        return (
            _load_primary_config(db)
            if endpoint == "primary"
            else _load_secondary_config(db)
        )

    def endpoint_ready(self, db: Session, endpoint: AIEndpoint) -> bool:
        cfg = self.get_endpoint_config(db, endpoint)
        if not (cfg.base_url and cfg.model):
            return False
        return not (cfg.require_api_key and not cfg.api_key)

    def circuit_state(self, db: Session, endpoint: AIEndpoint) -> dict[str, Any]:
        cfg = self.get_endpoint_config(db, endpoint)
        state = self._get_state(cfg, endpoint)
        now = self._now()
        is_open = bool(state.cooldown_until and now < state.cooldown_until)
        open_duration_seconds = 0.0
        if state.opened_at is not None and is_open:
            open_duration_seconds = max((now - state.opened_at).total_seconds(), 0.0)
        cooldown_remaining_seconds = 0.0
        if state.cooldown_until and now < state.cooldown_until:
            cooldown_remaining_seconds = max(
                (state.cooldown_until - now).total_seconds(), 0.0
            )
        return {
            "endpoint": endpoint,
            "provider": cfg.label,
            "model": cfg.model,
            "configured": bool(cfg.base_url and cfg.model),
            "is_open": is_open,
            "consecutive_failures": state.consecutive_failures,
            "cooldown_until": state.cooldown_until.isoformat()
            if state.cooldown_until
            else None,
            "cooldown_remaining_seconds": cooldown_remaining_seconds,
            "open_since": state.opened_at.isoformat() if state.opened_at else None,
            "open_duration_seconds": open_duration_seconds,
        }

    def mark_endpoint_healthy(self, db: Session, endpoint: AIEndpoint) -> None:
        cfg = self.get_endpoint_config(db, endpoint)
        self._record_success(cfg, endpoint)

    def _client_for(self, cfg: AIEndpointConfig) -> VllmClient:
        return VllmClient(
            provider=cfg.label,
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
            timeout_seconds=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
        )

    def generate(
        self,
        db: Session,
        *,
        endpoint: AIEndpoint,
        system: str,
        prompt: str,
        max_tokens: int | None = None,
    ) -> AIResponse:
        if not self.enabled(db):
            raise AIClientError(
                "AI features are disabled (integration.ai_enabled=false)",
                failure_type="ai_disabled",
            )

        cfg = (
            _load_primary_config(db)
            if endpoint == "primary"
            else _load_secondary_config(db)
        )
        if not (cfg.base_url and cfg.model):
            raise AIClientError(f"AI endpoint not configured: {endpoint}")
        if cfg.require_api_key and not cfg.api_key:
            raise AIClientError(f"AI endpoint requires an API key: {endpoint}")

        self._before_request(cfg, endpoint)
        effective_max_tokens = min(
            int(max_tokens or cfg.max_tokens), int(cfg.max_tokens)
        )
        client = self._client_for(cfg)
        try:
            result = client.generate(system, prompt, max_tokens=effective_max_tokens)
        except AIClientError as exc:
            self._record_failure(cfg, endpoint, exc)
            raise
        self._record_success(cfg, endpoint)
        return result

    def generate_with_fallback(
        self,
        db: Session,
        *,
        primary: AIEndpoint = "primary",
        fallback: AIEndpoint = "secondary",
        system: str,
        prompt: str,
        max_tokens: int | None = None,
    ) -> tuple[AIResponse, dict[str, Any]]:
        """
        Try primary; if it fails and fallback is configured, try fallback.
        Returns (result, metadata) where metadata indicates whether fallback was used.
        """
        if not self.enabled(db):
            raise AIClientError(
                "AI features are disabled (integration.ai_enabled=false)",
                failure_type="ai_disabled",
            )
        try:
            result = self.generate(
                db,
                endpoint=primary,
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
            )
            return result, {"endpoint": primary, "fallback_used": False}
        except AIClientError as exc:
            if exc.failure_type == "ai_disabled":
                raise
            logger.warning(
                "AI primary endpoint failed (%s). Trying fallback. provider=%s model=%s "
                "failure_type=%s status=%s timeout_type=%s retry_count=%s request_id=%s",
                primary,
                exc.provider,
                exc.model,
                exc.failure_type,
                exc.status_code,
                exc.timeout_type,
                exc.retry_count,
                exc.request_id,
            )
            if not self.endpoint_ready(db, fallback):
                logger.warning(
                    "AI fallback endpoint unavailable primary=%s fallback=%s primary_failure_type=%s",
                    primary,
                    fallback,
                    exc.failure_type,
                )
                raise
            observe_ai_provider_fallback(
                from_endpoint=primary, to_endpoint=fallback, reason=exc.failure_type
            )
            result = self.generate(
                db,
                endpoint=fallback,
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
            )
            return result, {
                "endpoint": fallback,
                "fallback_used": True,
                "primary_error": redact_secret_text(exc),
            }


ai_gateway = AIGateway()
