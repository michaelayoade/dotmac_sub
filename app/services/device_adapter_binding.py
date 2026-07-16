"""Version-aware device identity and adapter binding contract.

Control-plane operations pin this binding when they are planned and resolve it
again immediately before dispatch. A changed device identity or capability
profile therefore stops the write instead of silently selecting new behavior.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ADAPTER_BINDING_PAYLOAD_KEY = "_adapter_binding"
ADAPTER_BINDING_SCHEMA_VERSION = 1


class AdapterBindingError(ValueError):
    """Base error for invalid or stale adapter bindings."""


class AdapterIdentityChanged(AdapterBindingError):
    """The dispatch-time identity/profile differs from the planned binding."""


def _clean(value: object | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split()).casefold()


def stable_revision(value: object) -> str:
    """Return a deterministic revision for JSON-compatible profile data."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DeviceIdentity:
    """Material hardware/software identity used to select an adapter."""

    vendor: str
    model: str
    firmware_version: str | None = None
    software_version: str | None = None
    hardware_revision: str | None = None
    data_model: str | None = None
    architecture: str | None = None

    def __post_init__(self) -> None:
        vendor = _clean(self.vendor)
        model = _clean(self.model)
        if not vendor or not model:
            raise AdapterBindingError("Device vendor and model are required")
        object.__setattr__(self, "vendor", vendor)
        object.__setattr__(self, "model", model)
        for field_name in (
            "firmware_version",
            "software_version",
            "hardware_revision",
            "data_model",
            "architecture",
        ):
            object.__setattr__(self, field_name, _clean(getattr(self, field_name)))

    @classmethod
    def from_device(
        cls,
        device: object,
        *,
        vendor: str | None = None,
        model: str | None = None,
    ) -> DeviceIdentity:
        """Project domain-owned device rows onto the common identity contract."""
        return cls(
            vendor=vendor or _clean(getattr(device, "vendor", None)) or "",
            model=(
                model
                or _clean(getattr(device, "model", None))
                or _clean(getattr(device, "board_name", None))
                or ""
            ),
            firmware_version=(
                _clean(getattr(device, "firmware_version", None))
                or _clean(getattr(device, "routeros_version", None))
            ),
            software_version=_clean(getattr(device, "software_version", None)),
            hardware_revision=(
                _clean(getattr(device, "hardware_revision", None))
                or _clean(getattr(device, "hardware_version", None))
            ),
            data_model=_clean(getattr(device, "tr069_data_model", None)),
            architecture=_clean(getattr(device, "architecture", None)),
        )

    def as_payload(self) -> dict[str, str | None]:
        return {
            "vendor": self.vendor,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "software_version": self.software_version,
            "hardware_revision": self.hardware_revision,
            "data_model": self.data_model,
            "architecture": self.architecture,
        }

    def normalized_payload(self) -> dict[str, str | None]:
        return {key: _normalized(value) for key, value in self.as_payload().items()}

    @property
    def fingerprint(self) -> str:
        return stable_revision(self.normalized_payload())


@dataclass(frozen=True)
class AdapterBinding:
    """Exact adapter/profile selected for one observed device identity."""

    adapter_name: str
    adapter_revision: str
    identity: DeviceIdentity
    capability_id: str | None = None
    capability_revision: str | None = None

    def __post_init__(self) -> None:
        adapter_name = _clean(self.adapter_name)
        adapter_revision = _clean(self.adapter_revision)
        if not adapter_name or not adapter_revision:
            raise AdapterBindingError("Adapter name and revision are required")
        object.__setattr__(self, "adapter_name", adapter_name)
        object.__setattr__(self, "adapter_revision", adapter_revision)
        object.__setattr__(self, "capability_id", _clean(self.capability_id))
        object.__setattr__(
            self, "capability_revision", _clean(self.capability_revision)
        )

    def material_payload(self) -> dict[str, object]:
        return {
            "adapter_name": self.adapter_name,
            "adapter_revision": self.adapter_revision,
            "capability_id": self.capability_id,
            "capability_revision": self.capability_revision,
            "identity": self.identity.normalized_payload(),
        }

    @property
    def fingerprint(self) -> str:
        return stable_revision(self.material_payload())

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": ADAPTER_BINDING_SCHEMA_VERSION,
            "adapter_name": self.adapter_name,
            "adapter_revision": self.adapter_revision,
            "capability_id": self.capability_id,
            "capability_revision": self.capability_revision,
            "identity": self.identity.as_payload(),
            "identity_fingerprint": self.identity.fingerprint,
            "binding_fingerprint": self.fingerprint,
        }


def attach_adapter_binding(
    payload: Mapping[str, Any] | None,
    binding: AdapterBinding,
) -> dict[str, Any]:
    """Return an operation payload containing the immutable planned binding."""
    result = dict(payload or {})
    result[ADAPTER_BINDING_PAYLOAD_KEY] = binding.as_payload()
    return result


def pinned_adapter_binding(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = (payload or {}).get(ADAPTER_BINDING_PAYLOAD_KEY)
    if not isinstance(raw, Mapping):
        raise AdapterBindingError("Operation has no pinned adapter binding")
    if raw.get("schema_version") != ADAPTER_BINDING_SCHEMA_VERSION:
        raise AdapterBindingError("Operation adapter binding schema is unsupported")
    return raw


def assert_adapter_binding(
    payload: Mapping[str, Any] | None,
    current: AdapterBinding,
) -> None:
    """Fail closed when dispatch would use a different identity or profile."""
    pinned = pinned_adapter_binding(payload)
    planned_fingerprint = _clean(pinned.get("binding_fingerprint"))
    if planned_fingerprint != current.fingerprint:
        planned_name = _clean(pinned.get("adapter_name")) or "unknown"
        planned_revision = _clean(pinned.get("adapter_revision")) or "unknown"
        raise AdapterIdentityChanged(
            "Device identity or adapter capability changed after planning; "
            f"planned {planned_name}@{planned_revision}, resolved "
            f"{current.adapter_name}@{current.adapter_revision}"
        )
