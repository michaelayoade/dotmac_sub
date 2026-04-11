"""Compatibility exports for ONT serial normalization and matching helpers."""

from __future__ import annotations

from app.services.network.ont_serials import (
    is_plausible_vendor_serial,
    looks_synthetic_ont_serial,
    normalize_ont_serial,
    prefer_ont_candidate,
)

_is_plausible_vendor_serial = is_plausible_vendor_serial
_looks_synthetic_ont_serial = looks_synthetic_ont_serial
_normalize_ont_serial = normalize_ont_serial
_prefer_ont_candidate = prefer_ont_candidate
