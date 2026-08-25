"""Transport-neutral errors for the adoption coordinator."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class AdoptionErrorCode(StrEnum):
    INVALID_SOURCE_FACT = "billing_adoption.invalid_source_fact"
    INCOHERENT_WATERMARK = "billing_adoption.incoherent_watermark"
    SHADOW_TOPOLOGY_UNSAFE = "billing_adoption.shadow_topology_unsafe"
    UNKNOWN_ACCOUNT = "billing_adoption.unknown_account"
    MIXED_TENANT_BUNDLE = "billing_adoption.mixed_tenant_bundle"
    DUPLICATE_RECONCILIATION_OBSERVATION = (
        "billing_adoption.duplicate_reconciliation_observation"
    )
    INVALID_DRIFT_ACCEPTANCE = "billing_adoption.invalid_drift_acceptance"


class BillingAdoptionError(Exception):
    """Stable fail-closed rejection from the adoption boundary."""

    __slots__ = ("code", "context", "message")

    def __init__(
        self,
        code: AdoptionErrorCode,
        message: str,
        *,
        context: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.context: Mapping[str, str] = MappingProxyType(dict(context or {}))
        super().__init__(message)


__all__ = ["AdoptionErrorCode", "BillingAdoptionError"]
