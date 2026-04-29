"""Service package exports."""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_MODULES = {
    "genieacs_service",
    "genieacs_service_intent",
    "audit_adapter",
    "billing_adapter",
    "contact",
    "db_session_adapter",
    "external_bss_adapter",
    "ipam_adapter",
    "notification_adapter",
    "olt_action_adapter",
    "olt_detail_adapter",
    "olt_observed_state_adapter",
    "olt_profile_adapter",
    "payment_gateway_adapter",
    "queue_adapter",
    "queue_strategy_adapter",
    "rate_limiter_adapter",
    "service_intent_adapter",
    "service_intent_ui_adapter",
    "web_network_olts",
}

__all__ = sorted(_LAZY_MODULES)


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        return importlib.import_module(f"app.services.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
