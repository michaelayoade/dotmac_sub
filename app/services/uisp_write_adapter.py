"""Schema-driven UISP configuration write and readback adapter."""

from __future__ import annotations

import copy
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import CPEDevice, OntUnit, VendorModelCapability
from app.models.uisp_control import UispDeviceIntent, UispIntentTargetType
from app.services.credential_crypto import decrypt_credential
from app.services.network.vendor_capabilities import vendor_capabilities
from app.services.uisp import (
    UispApiError,
    UispClient,
    UispClientError,
    UispUnsupportedOperationError,
)
from app.services.uisp_control_plane import redact_config

_CANONICAL_FIELDS = {
    "name",
    "management_ip",
    "firmware_version",
    "wifi.ssid",
    "wifi.password_ref",
    "remote_access.enabled",
    "lifecycle.state",
}
_SECRET_FIELDS = {"wifi.password_ref"}


class UispWriteAdapterError(RuntimeError):
    pass


class UispWriteUnsupported(UispWriteAdapterError):
    pass


class UispPostWriteReadbackError(UispWriteAdapterError):
    """UISP accepted a write, but mandatory readback could not complete."""


@dataclass(frozen=True)
class UispFieldMapping:
    canonical_name: str
    path: str
    readback_path: str
    create: bool = False
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UispCapabilityProfile:
    vendor: str
    model: str
    transport: str
    writable_fields: tuple[str, ...]
    requested_fields: tuple[str, ...]
    unsupported_fields: tuple[str, ...]

    @property
    def apply_ready(self) -> bool:
        return bool(self.requested_fields) and not self.unsupported_fields


@dataclass
class UispApplyResult:
    outcome: str
    message: str
    write_accepted: bool = False
    verified: bool = False
    attempts: int = 0
    observed_config: dict[str, Any] = field(default_factory=dict)
    drift: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "message": self.message,
            "write_accepted": self.write_accepted,
            "verified": self.verified,
            "attempts": self.attempts,
            "observed_config": redact_config(self.observed_config),
            "drift": redact_config(self.drift),
            "response": redact_config(self.response),
        }


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise UispWriteUnsupported("UISP field paths must be JSON pointers")
    return [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]


def _pointer_get(document: Any, pointer: str) -> Any:
    current = document
    for part in _pointer_parts(pointer):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _pointer_set(document: Any, pointer: str, value: Any, *, create: bool) -> None:
    parts = _pointer_parts(pointer)
    if not parts:
        raise UispWriteUnsupported("Root configuration replacement is not allowed")
    current = document
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif create and isinstance(current, dict):
            current[part] = {}
            current = current[part]
        else:
            raise UispWriteUnsupported(
                f"Mapped UISP configuration parent does not exist: {pointer}"
            )
    leaf = parts[-1]
    if isinstance(current, dict):
        if leaf not in current and not create:
            raise UispWriteUnsupported(
                f"Mapped UISP configuration field does not exist: {pointer}"
            )
        current[leaf] = value
        return
    if isinstance(current, list) and leaf.isdigit() and int(leaf) < len(current):
        current[int(leaf)] = value
        return
    raise UispWriteUnsupported(f"Mapped UISP configuration field is invalid: {pointer}")


def _desired_fields(desired: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("name", "management_ip", "firmware_version"):
        if key in desired:
            fields[key] = desired[key]
    wifi = desired.get("wifi")
    if isinstance(wifi, dict):
        for key, value in wifi.items():
            fields[f"wifi.{key}"] = value
    remote_access = desired.get("remote_access")
    if isinstance(remote_access, dict):
        for key, value in remote_access.items():
            fields[f"remote_access.{key}"] = value
    lifecycle = desired.get("lifecycle")
    if isinstance(lifecycle, dict):
        for key, value in lifecycle.items():
            fields[f"lifecycle.{key}"] = value
    return fields


def _resolve_target(
    db: Session, intent: UispDeviceIntent
) -> tuple[CPEDevice | OntUnit, str, str]:
    target: CPEDevice | OntUnit | None
    if intent.target_type == UispIntentTargetType.cpe:
        target = db.get(CPEDevice, intent.target_id)
    else:
        target = db.get(OntUnit, intent.target_id)
    if target is None:
        raise UispWriteUnsupported("UISP intent target no longer exists")
    if not target.uisp_device_id:
        raise UispWriteUnsupported("UISP target has not been adopted")
    vendor = str(target.vendor or "").strip()
    model = str(target.model or "").strip()
    if not vendor or not model:
        raise UispWriteUnsupported("UISP target vendor and model are required")
    return target, vendor, model


def _uisp_capability(
    db: Session, *, vendor: str, model: str, firmware: str | None
) -> tuple[VendorModelCapability, dict[str, Any]]:
    capability = vendor_capabilities.resolve_capability(
        db, vendor=vendor, model=model, firmware=firmware
    )
    if capability is None:
        raise UispWriteUnsupported(
            f"No active UISP capability mapping for {vendor} {model}"
        )
    features = capability.supported_features or {}
    config = features.get("uisp") if isinstance(features, dict) else None
    if not isinstance(config, dict) or config.get("configuration_write") is not True:
        raise UispWriteUnsupported(
            f"UISP configuration writes are not enabled for {vendor} {model}"
        )
    if config.get("transport") not in {"airos", "onu"}:
        raise UispWriteUnsupported(
            "UISP configuration transport must be explicitly mapped as airos or onu"
        )
    return capability, config


def _field_mappings(
    config: dict[str, Any], desired_fields: dict[str, Any]
) -> dict[str, UispFieldMapping]:
    raw_fields = config.get("fields")
    if not isinstance(raw_fields, dict):
        raise UispWriteUnsupported("UISP capability has no field mappings")
    mappings: dict[str, UispFieldMapping] = {}
    for canonical_name in desired_fields:
        if canonical_name not in _CANONICAL_FIELDS:
            raise UispWriteUnsupported(f"Unsupported UISP field: {canonical_name}")
        raw = raw_fields.get(canonical_name)
        if isinstance(raw, str):
            raw = {"path": raw}
        if not isinstance(raw, dict) or raw.get("writable", True) is not True:
            raise UispWriteUnsupported(
                f"UISP field is not mapped writable: {canonical_name}"
            )
        path = str(raw.get("path") or "").strip()
        readback_path = str(raw.get("readback_path") or path).strip()
        _pointer_parts(path)
        _pointer_parts(readback_path)
        raw_values = raw.get("values")
        values: dict[str, Any] = raw_values if isinstance(raw_values, dict) else {}
        mappings[canonical_name] = UispFieldMapping(
            canonical_name=canonical_name,
            path=path,
            readback_path=readback_path,
            create=raw.get("create") is True,
            values={str(key): value for key, value in values.items()},
        )
    return mappings


def capability_profile(
    db: Session,
    intent: UispDeviceIntent,
    *,
    desired_state: dict[str, Any] | None = None,
) -> UispCapabilityProfile:
    """Resolve the exact writable/readable field contract for an intent target."""
    _target, vendor, model = _resolve_target(db, intent)
    firmware = str(getattr(_target, "firmware_version", "") or "") or None
    _capability, config = _uisp_capability(
        db, vendor=vendor, model=model, firmware=firmware
    )
    raw_fields = config.get("fields")
    if not isinstance(raw_fields, dict):
        raise UispWriteUnsupported("UISP capability has no field mappings")
    writable_fields: list[str] = []
    for canonical_name, raw in raw_fields.items():
        if canonical_name not in _CANONICAL_FIELDS:
            raise UispWriteUnsupported(
                f"UISP capability contains an unknown field: {canonical_name}"
            )
        if isinstance(raw, dict) and raw.get("writable", True) is not True:
            continue
        _field_mappings(config, {canonical_name: None})
        writable_fields.append(canonical_name)
    desired = desired_state if desired_state is not None else intent.desired_state
    requested = tuple(sorted(_desired_fields(desired or {})))
    writable = tuple(sorted(writable_fields))
    return UispCapabilityProfile(
        vendor=vendor,
        model=model,
        transport=str(config["transport"]),
        writable_fields=writable,
        requested_fields=requested,
        unsupported_fields=tuple(sorted(set(requested) - set(writable))),
    )


def require_apply_ready(db: Session, intent: UispDeviceIntent) -> UispCapabilityProfile:
    profile = capability_profile(db, intent)
    if not profile.requested_fields:
        raise UispWriteUnsupported("UISP intent has no writable fields")
    if profile.unsupported_fields:
        raise UispWriteUnsupported(
            "UISP desired state contains fields not mapped for "
            f"{profile.vendor} {profile.model}: "
            + ", ".join(profile.unsupported_fields)
        )
    return profile


def _materialize_value(
    canonical_name: str, value: Any, mapping: UispFieldMapping
) -> Any:
    if canonical_name in _SECRET_FIELDS:
        resolved = decrypt_credential(str(value))
        if not resolved:
            raise UispWriteAdapterError("UISP Wi-Fi credential could not be resolved")
        value = resolved
    if mapping.values:
        key = str(value).lower() if isinstance(value, bool) else str(value)
        if key not in mapping.values:
            raise UispWriteUnsupported(
                f"UISP field value is not mapped: {canonical_name}={value}"
            )
        value = mapping.values[key]
    return value


def _values_equal(canonical_name: str, expected: Any, actual: Any) -> bool:
    if canonical_name in _SECRET_FIELDS:
        return secrets.compare_digest(str(expected), str(actual))
    return expected == actual


class UispConfigurationWriteAdapter:
    def __init__(
        self,
        client: UispClient,
        *,
        readback_attempts: int = 5,
        readback_delay_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.readback_attempts = max(1, min(int(readback_attempts), 10))
        self.readback_delay_seconds = max(0.0, readback_delay_seconds)
        self.sleeper = sleeper

    def apply(self, db: Session, intent: UispDeviceIntent) -> UispApplyResult:
        target, device_id, transport, mappings, expected = self._prepare(db, intent)
        original = self.client.get_device_configuration(device_id, transport=transport)
        proposed = copy.deepcopy(original)
        for canonical_name, value in expected.items():
            mapping = mappings[canonical_name]
            _pointer_set(proposed, mapping.path, value, create=mapping.create)

        if proposed == original:
            return self._readback(
                device_id,
                transport,
                mappings,
                expected,
                write_accepted=False,
                response={"unchanged": True},
            )

        try:
            response = self.client.put_device_configuration(
                device_id, proposed, transport=transport
            )
        except UispUnsupportedOperationError as exc:
            raise UispWriteUnsupported(
                f"UISP does not implement configuration writes for {target.model}: {exc}"
            ) from exc
        except UispApiError as exc:
            raise UispWriteAdapterError(
                f"UISP rejected configuration write with HTTP {exc.status_code}: {exc}"
            ) from exc
        except UispClientError as exc:
            raise UispWriteAdapterError(str(exc)) from exc

        try:
            return self._readback(
                device_id,
                transport,
                mappings,
                expected,
                write_accepted=True,
                response=response,
            )
        except Exception as exc:
            # The write has already hit the device. Past this point *any*
            # readback failure is a post-write readback failure -- not just a
            # UispClientError. An unexpected device payload raising KeyError or
            # TypeError in the comparison would otherwise escape untranslated,
            # reach the task's generic handler with ``result`` still None, and
            # be recorded as a plain `failed` intent. `failed` is not in the
            # reconciliation filter, so the device would sit silently diverged
            # from its desired_state with nothing left to notice.
            raise UispPostWriteReadbackError(
                "UISP accepted the write but device readback failed"
            ) from exc

    def readback(self, db: Session, intent: UispDeviceIntent) -> UispApplyResult:
        """Read and compare mapped fields without issuing a write."""
        _target, device_id, transport, mappings, expected = self._prepare(db, intent)
        return self._readback(
            device_id,
            transport,
            mappings,
            expected,
            write_accepted=False,
            response=None,
        )

    def _prepare(
        self, db: Session, intent: UispDeviceIntent
    ) -> tuple[
        CPEDevice | OntUnit,
        str,
        str,
        dict[str, UispFieldMapping],
        dict[str, Any],
    ]:
        target, vendor, model = _resolve_target(db, intent)
        device_id = target.uisp_device_id
        if not device_id:
            raise UispWriteUnsupported("UISP target has not been adopted")
        firmware = str(getattr(target, "firmware_version", "") or "") or None
        _capability, config_spec = _uisp_capability(
            db, vendor=vendor, model=model, firmware=firmware
        )
        transport = str(config_spec["transport"])
        desired_fields = _desired_fields(intent.desired_state or {})
        if not desired_fields:
            raise UispWriteUnsupported("UISP intent has no writable fields")
        mappings = _field_mappings(config_spec, desired_fields)
        expected: dict[str, Any] = {}
        for canonical_name, desired_value in desired_fields.items():
            mapping = mappings[canonical_name]
            value = _materialize_value(canonical_name, desired_value, mapping)
            expected[canonical_name] = value
        return target, device_id, transport, mappings, expected

    def _readback(
        self,
        device_id: str,
        transport: str,
        mappings: dict[str, UispFieldMapping],
        expected: dict[str, Any],
        *,
        write_accepted: bool,
        response: dict[str, Any] | None,
    ) -> UispApplyResult:
        last_observed: dict[str, Any] = {}
        last_drift: dict[str, Any] = {}
        for attempt in range(1, self.readback_attempts + 1):
            if attempt > 1 and self.readback_delay_seconds:
                self.sleeper(self.readback_delay_seconds)
            document = self.client.get_device_configuration(
                device_id, transport=transport
            )
            observed: dict[str, Any] = {}
            drift: dict[str, Any] = {}
            for canonical_name, mapping in mappings.items():
                try:
                    actual = _pointer_get(document, mapping.readback_path)
                except KeyError:
                    drift[canonical_name] = {
                        "desired": "[redacted]"
                        if canonical_name in _SECRET_FIELDS
                        else expected[canonical_name],
                        "observed": "missing",
                    }
                    continue
                matched = _values_equal(
                    canonical_name, expected[canonical_name], actual
                )
                if canonical_name in _SECRET_FIELDS:
                    observed[canonical_name] = "[verified]" if matched else "[mismatch]"
                else:
                    observed[canonical_name] = actual
                if not matched:
                    drift[canonical_name] = {
                        "desired": "[redacted]"
                        if canonical_name in _SECRET_FIELDS
                        else expected[canonical_name],
                        "observed": "[mismatch]"
                        if canonical_name in _SECRET_FIELDS
                        else actual,
                    }
            last_observed, last_drift = observed, drift
            if not drift:
                return UispApplyResult(
                    outcome="verified",
                    message="UISP configuration matched device readback",
                    write_accepted=write_accepted,
                    verified=True,
                    attempts=attempt,
                    observed_config=observed,
                    response=response,
                )
        return UispApplyResult(
            outcome="drifted",
            message=(
                "UISP accepted the write but device readback did not converge"
                if write_accepted
                else "UISP device readback does not match desired configuration"
            ),
            write_accepted=write_accepted,
            verified=False,
            attempts=self.readback_attempts,
            observed_config=last_observed,
            drift=last_drift,
            response=response,
        )
