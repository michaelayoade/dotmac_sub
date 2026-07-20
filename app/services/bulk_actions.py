"""Code-native UI contracts for bulk selection and action presentation.

The contracts in this module describe interaction capabilities. Domain command
services continue to own authorization, eligibility, mutation, audit, and side
effects, and must re-check those decisions when a command executes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

BulkActionTone = Literal["positive", "info", "warning", "negative", "neutral"]
BulkExecutionMode = Literal["synchronous", "queued"]
BulkSelectionMode = Literal["selected", "filtered"]


def membership_scope_token(scope: str, member_ids: Collection[str]) -> str:
    """Fingerprint exact bulk membership independently of row ordering."""

    normalized_ids = sorted({str(member_id).strip() for member_id in member_ids})
    material = "\0".join((str(scope).strip(), *normalized_ids)).encode()
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class BulkActionDefinition:
    """One backend-declared bulk action capability."""

    key: str
    label: str
    description: str
    permission: str
    tone: BulkActionTone = "neutral"
    requires_preview: bool = True
    requires_confirmation: bool = True
    execution_mode: BulkExecutionMode = "synchronous"
    result_reference: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("key", "label", "description", "permission"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"Bulk action {field_name} is required")
        if self.execution_mode == "queued" and not self.result_reference:
            raise ValueError("Queued bulk actions must declare a result reference")


@dataclass(frozen=True, slots=True)
class BulkResourceDefinition:
    """Selection behavior and action capabilities for one list resource."""

    key: str
    actions: tuple[BulkActionDefinition, ...]
    filtered_selection_supported: bool = False
    select_all_scope: Literal["page"] = "page"
    query_change_behavior: Literal["clear"] = "clear"

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Bulk resource key is required")
        action_keys = tuple(action.key for action in self.actions)
        if len(set(action_keys)) != len(action_keys):
            raise ValueError(f"Duplicate bulk action keys for {self.key}")

    def project(self, *, authorized_permissions: Collection[str]) -> BulkActionContract:
        """Omit unauthorized actions and expose no permission vocabulary."""

        allowed = set(authorized_permissions)
        actions = tuple(
            BulkActionProjection(
                key=action.key,
                label=action.label,
                description=action.description,
                tone=action.tone,
                requires_preview=action.requires_preview,
                requires_confirmation=action.requires_confirmation,
                execution_mode=action.execution_mode,
                result_reference=action.result_reference,
            )
            for action in self.actions
            if action.permission in allowed
        )
        return BulkActionContract(
            resource_key=self.key,
            actions=actions,
            select_all_scope=self.select_all_scope,
            filtered_selection_supported=self.filtered_selection_supported,
            query_change_behavior=self.query_change_behavior,
        )


@dataclass(frozen=True, slots=True)
class BulkActionProjection:
    """Transport-safe presentation of one authorized action."""

    key: str
    label: str
    description: str
    tone: BulkActionTone
    requires_preview: bool
    requires_confirmation: bool
    execution_mode: BulkExecutionMode
    result_reference: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "tone": self.tone,
            "requires_preview": self.requires_preview,
            "requires_confirmation": self.requires_confirmation,
            "execution_mode": self.execution_mode,
            "result_reference": self.result_reference,
        }


@dataclass(frozen=True, slots=True)
class BulkActionContract:
    """Authorized bulk interaction projection for one resource."""

    resource_key: str
    actions: tuple[BulkActionProjection, ...]
    select_all_scope: Literal["page"]
    filtered_selection_supported: bool
    query_change_behavior: Literal["clear"]

    @property
    def selection_enabled(self) -> bool:
        return bool(self.actions)

    def as_dict(self) -> dict[str, object]:
        return {
            "resource_key": self.resource_key,
            "selection_enabled": self.selection_enabled,
            "select_all_scope": self.select_all_scope,
            "filtered_selection_supported": self.filtered_selection_supported,
            "query_change_behavior": self.query_change_behavior,
            "actions": [action.as_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class BulkSelection:
    """Normalized, explicit selection submitted to a command adapter."""

    mode: BulkSelectionMode
    ids: tuple[str, ...] = ()
    filters: tuple[tuple[str, str], ...] = ()
    expected_count: int | None = None
    expected_scope_token: str | None = None

    def filter_value(self, key: str) -> str | None:
        return dict(self.filters).get(key)


def _normalize_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("selection.ids must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        raw_id = item.get("id") if isinstance(item, Mapping) else item
        item_id = str(raw_id or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        normalized.append(item_id)
    return tuple(normalized)


def _normalize_expected_count(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        expected = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("selection.expected_count must be a whole number") from exc
    if expected < 0:
        raise ValueError("selection.expected_count cannot be negative")
    return expected


def parse_bulk_selection(
    payload: Mapping[str, object],
    *,
    allowed_filter_keys: Collection[str],
    filtered_selection_supported: bool,
    legacy_id_key: str | None = None,
) -> BulkSelection:
    """Parse an explicit selected-ID or filtered-query selection.

    A non-empty legacy ID list remains an explicit selected scope during the
    migration. Missing or empty IDs never fall through to a filtered cohort.
    """

    raw_selection = payload.get("selection")
    if raw_selection is None and legacy_id_key:
        legacy_ids = payload.get(legacy_id_key)
        if legacy_ids:
            ids = _normalize_ids(legacy_ids)
            if ids:
                return BulkSelection(mode="selected", ids=ids)

    if not isinstance(raw_selection, Mapping):
        raise ValueError("Select at least one record before using a bulk action")

    mode = str(raw_selection.get("mode") or "").strip().lower()
    expected_count = _normalize_expected_count(raw_selection.get("expected_count"))
    expected_scope_token = (
        str(raw_selection.get("expected_scope_token") or "").strip() or None
    )
    if mode == "selected":
        ids = _normalize_ids(raw_selection.get("ids"))
        if not ids:
            raise ValueError("Select at least one record before using a bulk action")
        return BulkSelection(
            mode="selected",
            ids=ids,
            expected_count=expected_count,
            expected_scope_token=expected_scope_token,
        )

    if mode != "filtered":
        raise ValueError("selection.mode must be selected or filtered")
    if not filtered_selection_supported:
        raise ValueError("Filtered bulk selection is not supported for this resource")

    raw_filters = raw_selection.get("filters")
    if not isinstance(raw_filters, Mapping):
        raise ValueError("selection.filters must be an object")
    allowed = set(allowed_filter_keys)
    unknown = sorted(str(key) for key in raw_filters if str(key) not in allowed)
    if unknown:
        raise ValueError("Unsupported selection filters: " + ", ".join(unknown))
    filters = tuple(
        (key, normalized)
        for key in allowed_filter_keys
        if (normalized := str(raw_filters.get(key) or "").strip())
    )
    return BulkSelection(
        mode="filtered",
        filters=filters,
        expected_count=expected_count,
        expected_scope_token=expected_scope_token,
    )
