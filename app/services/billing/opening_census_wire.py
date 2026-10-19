"""Census preview -> rehearsal census wire document (Step 5).

Serializes the typed opening census preview
(``BillingOpeningExportPreviewV3``) to the exact JSON document Starter's
read-only rehearsal adapter consumes (``sub-billing-opening-census-v2`` /
``-v3``), field for field under the dataclass field names. This is pure
evidence rendering: it verifies nothing, qualifies no cash, invents no
originals, and writes no money state.

Wire conversions are exact and fail closed: Decimals render as plain
decimal strings (never JSON numbers, never exponents, non-finite values
refused), datetimes must be timezone-aware and render in UTC ISO format,
enums render as their values, and any value outside the closed evidence
vocabulary raises instead of being coerced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from app.services.billing.opening_export_contracts import (
    BillingOpeningExportPreviewV3,
)
from app.services.domain_errors import DomainError

__all__ = ["OpeningCensusWireError", "render_opening_census_wire"]

_OWNER = "financial.invoices.opening_census_wire"
_WIRE_POPULATION_VERSIONS = frozenset(
    {"sub-billing-opening-census-v2", "sub-billing-opening-census-v3"}
)


class OpeningCensusWireError(DomainError, ValueError):
    """The census preview cannot be rendered to the wire truthfully."""


def _fail(code: str, message: str) -> OpeningCensusWireError:
    return OpeningCensusWireError(
        code=f"{_OWNER}.{code}", message=message, retryable=False
    )


def _wire(value: object) -> object:
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _fail(
                "nonfinite_source",
                "Non-finite monetary evidence cannot be rendered to the census wire.",
            )
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise _fail(
                "naive_instant",
                "Naive instants cannot be rendered to the census wire.",
            )
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        enum_value = _wire(value.value)
        if type(enum_value) is not str:
            raise _fail(
                "unsupported_evidence",
                f"Unsupported census evidence value: {type(value).__name__}",
            )
        return enum_value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _wire(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    if isinstance(value, Mapping):
        rendered: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _fail(
                    "unsupported_evidence",
                    f"Unsupported census evidence key: {type(key).__name__}",
                )
            rendered[key] = _wire(item)
        return rendered
    raise _fail(
        "unsupported_evidence",
        f"Unsupported census evidence value: {type(value).__name__}",
    )


def render_opening_census_wire(preview: BillingOpeningExportPreviewV3) -> str:
    """Render one census preview as the rehearsal census wire document."""

    if not isinstance(preview, BillingOpeningExportPreviewV3):
        raise _fail(
            "unsupported_evidence",
            "The census wire renders BillingOpeningExportPreviewV3 only.",
        )
    population_version = preview.capture.declared_source.population_version
    if population_version not in _WIRE_POPULATION_VERSIONS:
        raise _fail(
            "unsupported_population_version",
            "The census wire renders census v2/v3 populations only.",
        )
    payload = _wire(preview)
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    )
