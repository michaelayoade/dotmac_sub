"""Admin page projection and entry edits for walled-garden allowed resources.

This module is a web form helper in the same position as
``web_system_settings_forms``: it is not an owner. It composes three owners
for the ``/admin/system/config/walled-garden`` page and submits edits to one:

* ``control.settings_spec`` resolves ``radius.walled_garden_allowed_resources``
  and ``app.schemas.walled_garden`` validates it;
* ``access.walled_garden_router_module`` derives the read-only portal entry
  from ``captive_portal_url``/``captive_portal_ip``;
* ``access.walled_garden_router_readiness`` supplies the per-router readiness
  and drift summary (snapshot-based, never contacts a router).

Every edit (add, change, enable, disable, remove) turns one entry change into
the complete validated setting value and submits it to
``control.settings_form_updates`` (``apply_admin_settings_form_updates``),
which owns validation re-checks, the transaction, the stale-value lock, and
the audit record. Nothing here commits or writes ORM state.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.schemas.walled_garden import (
    WALLED_GARDEN_PORTAL_ENTRY_KEY,
    WalledGardenAllowedResource,
    WalledGardenAllowedResources,
    WalledGardenResourceKind,
)
from app.services import db_session_adapter, domain_settings, settings_spec
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.settings_api_custom import (
    SettingNormalizationError,
    _normalize_spec_setting,
)
from app.services.walled_garden_router_module import (
    WalledGardenModuleError,
    render_walled_garden_module_from_settings,
)
from app.services.walled_garden_router_readiness import (
    DEFAULT_MAX_SNAPSHOT_AGE,
    FleetWalledGardenReadinessQuery,
    WalledGardenReadinessStatus,
    WalledGardenRouterReadiness,
    resolve_fleet_walled_garden_readiness,
)

logger = logging.getLogger(__name__)

SETTING_DOMAIN = SettingDomain.radius
SETTING_KEY = "walled_garden_allowed_resources"

#: Kinds an operator may choose; ``portal`` is reserved for the derived entry.
EDITABLE_KINDS: tuple[WalledGardenResourceKind, ...] = tuple(
    kind
    for kind in WalledGardenResourceKind
    if kind is not WalledGardenResourceKind.portal
)

_ERROR_PREFIX = "walled_garden_settings"
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
_HOST_SPLIT_RE = re.compile(r"[\s,]+")
_ATTENTION_ORDER = {
    WalledGardenReadinessStatus.not_configured: 0,
    WalledGardenReadinessStatus.not_ready: 1,
    WalledGardenReadinessStatus.stale: 2,
    WalledGardenReadinessStatus.no_snapshot: 3,
    WalledGardenReadinessStatus.ready: 4,
}
#: Label and ``status_badge`` variant per readiness status. Text always
#: accompanies the tone, so state is never communicated by colour alone.
_STATUS_PRESENTATION: dict[WalledGardenReadinessStatus, tuple[str, str]] = {
    WalledGardenReadinessStatus.ready: ("Ready", "positive"),
    WalledGardenReadinessStatus.not_ready: ("Not ready", "negative"),
    WalledGardenReadinessStatus.stale: ("Stale snapshot", "warning"),
    WalledGardenReadinessStatus.no_snapshot: ("No snapshot", "neutral"),
    WalledGardenReadinessStatus.not_configured: ("Not configured", "negative"),
}


class WalledGardenEditAction(StrEnum):
    added = "added"
    updated = "updated"
    enabled = "enabled"
    disabled = "disabled"
    removed = "removed"


class WalledGardenEntryField(StrEnum):
    key = "key"
    label = "label"
    kind = "kind"
    hosts = "hosts"


class WalledGardenFormMode(StrEnum):
    add = "add"
    edit = "edit"


class WalledGardenEditErrorCode(StrEnum):
    INVALID_ENTRY = f"{_ERROR_PREFIX}.invalid_entry"
    ENTRY_NOT_FOUND = f"{_ERROR_PREFIX}.entry_not_found"
    STALE = f"{_ERROR_PREFIX}.stale"
    STORED_VALUE_INVALID = f"{_ERROR_PREFIX}.stored_value_invalid"
    SAVE_REFUSED = f"{_ERROR_PREFIX}.save_refused"


class WalledGardenEditError(DomainError):
    """Safe, transport-neutral refusal of one entry edit."""

    def __init__(
        self,
        code: WalledGardenEditErrorCode,
        message: str,
        *,
        field_errors: Mapping[WalledGardenEntryField, str] | None = None,
    ) -> None:
        self.error_code = code
        self.field_errors: dict[WalledGardenEntryField, str] = dict(field_errors or {})
        super().__init__(
            code=code.value,
            message=message,
            details={
                "fields": sorted(item.value for item in self.field_errors),
            },
            retryable=False,
        )


# --------------------------------------------------------------------------
# Read projection


@dataclass(frozen=True, slots=True)
class WalledGardenEntryRow:
    key: str
    label: str
    kind: WalledGardenResourceKind
    hosts: tuple[str, ...]
    enabled: bool
    derived: bool


@dataclass(frozen=True, slots=True)
class WalledGardenPortalState:
    """The derived portal entry, or why it cannot be derived."""

    entry: WalledGardenEntryRow | None
    configuration_error: str | None


@dataclass(frozen=True, slots=True)
class WalledGardenRouterReadinessRow:
    router_id: uuid.UUID
    router_name: str
    status: WalledGardenReadinessStatus
    snapshot_captured_at: datetime | None
    missing_entry_keys: tuple[str, ...]
    drifted_entry_keys: tuple[str, ...]
    finding_count: int

    @property
    def needs_attention(self) -> bool:
        return self.status is not WalledGardenReadinessStatus.ready

    @property
    def status_label(self) -> str:
        return _STATUS_PRESENTATION[self.status][0]

    @property
    def status_variant(self) -> str:
        return _STATUS_PRESENTATION[self.status][1]


@dataclass(frozen=True, slots=True)
class WalledGardenStatusCount:
    status: WalledGardenReadinessStatus
    count: int

    @property
    def status_label(self) -> str:
        return _STATUS_PRESENTATION[self.status][0]

    @property
    def status_variant(self) -> str:
        return _STATUS_PRESENTATION[self.status][1]


@dataclass(frozen=True, slots=True)
class WalledGardenFleetSummary:
    """Per-router readiness; ``unavailable`` is distinct from zero routers."""

    available: bool
    evaluated_at: datetime | None = None
    module_version: str | None = None
    max_snapshot_age_hours: int = int(DEFAULT_MAX_SNAPSHOT_AGE.total_seconds() // 3600)
    status_counts: tuple[WalledGardenStatusCount, ...] = ()
    routers: tuple[WalledGardenRouterReadinessRow, ...] = ()
    configuration_error: str | None = None

    @property
    def router_count(self) -> int:
        return len(self.routers)

    @property
    def ready_router_count(self) -> int:
        return sum(1 for row in self.routers if not row.needs_attention)

    @property
    def routers_missing_enabled_entries(self) -> int:
        return sum(1 for row in self.routers if row.missing_entry_keys)

    @property
    def routers_carrying_disabled_entries(self) -> int:
        return sum(1 for row in self.routers if row.drifted_entry_keys)


@dataclass(frozen=True, slots=True)
class WalledGardenEntryFormState:
    mode: WalledGardenFormMode
    original_key: str | None = None
    key: str = ""
    label: str = ""
    kind: str = WalledGardenResourceKind.payment.value
    hosts_text: str = ""
    enabled: bool = False
    field_errors: Mapping[str, str] = field(default_factory=dict)
    form_error: str | None = None


@dataclass(frozen=True, slots=True)
class WalledGardenSettingsPage:
    entries: tuple[WalledGardenEntryRow, ...]
    portal: WalledGardenPortalState
    value_fingerprint: str
    stored_value_error: str | None
    last_changed_at: datetime | None
    fleet: WalledGardenFleetSummary
    form: WalledGardenEntryFormState
    kinds: tuple[WalledGardenResourceKind, ...] = EDITABLE_KINDS

    @property
    def enabled_entry_count(self) -> int:
        return sum(1 for entry in self.entries if entry.enabled)


def _current_resources(
    db: Session,
) -> tuple[WalledGardenAllowedResources | None, str | None]:
    value = settings_spec.resolve_value(db, SETTING_DOMAIN, SETTING_KEY)
    try:
        return WalledGardenAllowedResources.from_setting_value(value), None
    except (ValueError, TypeError) as exc:
        return None, f"The stored allowed-resource value is invalid: {exc}"


def _portal_state(db: Session) -> WalledGardenPortalState:
    try:
        module = render_walled_garden_module_from_settings(db)
    except WalledGardenModuleError as exc:
        return WalledGardenPortalState(entry=None, configuration_error=exc.message)
    portal = module.entry(WALLED_GARDEN_PORTAL_ENTRY_KEY)
    if portal is None:  # pragma: no cover - the module always derives it
        return WalledGardenPortalState(
            entry=None, configuration_error="Portal entry was not derived."
        )
    return WalledGardenPortalState(
        entry=WalledGardenEntryRow(
            key=portal.key,
            label=portal.label,
            kind=portal.kind,
            hosts=portal.hosts,
            enabled=portal.enabled,
            derived=True,
        ),
        configuration_error=None,
    )


def _readiness_row(item: WalledGardenRouterReadiness) -> WalledGardenRouterReadinessRow:
    return WalledGardenRouterReadinessRow(
        router_id=item.router_id,
        router_name=item.router_name,
        status=item.status,
        snapshot_captured_at=item.snapshot_captured_at,
        missing_entry_keys=item.missing_entry_keys,
        drifted_entry_keys=item.drifted_entry_keys,
        finding_count=len(item.findings),
    )


def build_fleet_summary(db: Session) -> WalledGardenFleetSummary:
    """Project the fleet readiness owner's outcome, attention-first."""

    try:
        fleet = resolve_fleet_walled_garden_readiness(
            db, query=FleetWalledGardenReadinessQuery()
        )
    except Exception:
        logger.exception(
            "walled_garden_settings_readiness_unavailable",
            extra={"event": "walled_garden_settings_readiness_unavailable"},
        )
        return WalledGardenFleetSummary(available=False)
    rows = sorted(
        (_readiness_row(item) for item in fleet.routers),
        key=lambda row: (_ATTENTION_ORDER[row.status], row.router_name.lower()),
    )
    configuration_error = next(
        (
            item.configuration_error
            for item in fleet.routers
            if item.configuration_error
        ),
        None,
    )
    return WalledGardenFleetSummary(
        available=True,
        evaluated_at=fleet.evaluated_at,
        module_version=fleet.module_version,
        status_counts=tuple(
            WalledGardenStatusCount(status=status, count=count)
            for status, count in fleet.status_counts.items()
        ),
        routers=tuple(rows),
        configuration_error=configuration_error,
    )


def _entry_row(entry: WalledGardenAllowedResource) -> WalledGardenEntryRow:
    return WalledGardenEntryRow(
        key=entry.key,
        label=entry.label,
        kind=entry.kind,
        hosts=entry.hosts,
        enabled=entry.enabled,
        derived=False,
    )


def _prefilled_form(
    entries: Sequence[WalledGardenEntryRow], edit_key: str | None
) -> WalledGardenEntryFormState:
    if edit_key:
        match = next((entry for entry in entries if entry.key == edit_key), None)
        if match is not None:
            return WalledGardenEntryFormState(
                mode=WalledGardenFormMode.edit,
                original_key=match.key,
                key=match.key,
                label=match.label,
                kind=match.kind.value,
                hosts_text="\n".join(match.hosts),
                enabled=match.enabled,
            )
    return WalledGardenEntryFormState(mode=WalledGardenFormMode.add)


def build_walled_garden_settings_page(
    db: Session,
    *,
    edit_key: str | None = None,
    form: WalledGardenEntryFormState | None = None,
) -> WalledGardenSettingsPage:
    """Compose the page from the settings, module, and readiness owners."""

    resources, stored_error = _current_resources(db)
    entries = (
        tuple(_entry_row(entry) for entry in resources.entries) if resources else ()
    )
    return WalledGardenSettingsPage(
        entries=entries,
        portal=_portal_state(db),
        value_fingerprint=domain_settings.admin_setting_value_fingerprint(
            db, domain=SETTING_DOMAIN, key=SETTING_KEY
        ),
        stored_value_error=stored_error,
        last_changed_at=settings_spec.active_setting_updated_at(
            db, SETTING_DOMAIN, SETTING_KEY
        ),
        fleet=build_fleet_summary(db),
        form=form if form is not None else _prefilled_form(entries, edit_key),
    )


# --------------------------------------------------------------------------
# Edit commands


@dataclass(frozen=True, slots=True)
class WalledGardenEntryDraft:
    """Operator input for one entry; validated by the schema, not here."""

    label: str
    kind: str
    hosts: tuple[str, ...]
    key: str = ""
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class SaveWalledGardenEntryCommand:
    context: CommandContext
    expected_fingerprint: str
    draft: WalledGardenEntryDraft
    #: ``None`` adds a new entry; otherwise the key of the entry being edited.
    #: Keys are immutable on edit because routers carry them in element tags.
    original_key: str | None = None


@dataclass(frozen=True, slots=True)
class SetWalledGardenEntryEnabledCommand:
    context: CommandContext
    expected_fingerprint: str
    key: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class RemoveWalledGardenEntryCommand:
    context: CommandContext
    expected_fingerprint: str
    key: str


@dataclass(frozen=True, slots=True)
class WalledGardenEditOutcome:
    action: WalledGardenEditAction
    entry_key: str
    updated_keys: tuple[str, ...]


def parse_hosts_text(text: str | None) -> tuple[str, ...]:
    """Split a textarea of hostnames (newline, comma, or space separated)."""

    return tuple(item for item in _HOST_SPLIT_RE.split(text or "") if item)


def suggested_entry_key(label: str) -> str:
    """Derive a key from a label when the operator leaves the key blank."""

    slug = _SLUG_STRIP_RE.sub("-", label.strip().lower()).strip("-")
    return slug[:40].strip("-")


def _field_message(message: str) -> str:
    return message.removeprefix("Value error, ")


def _validated_entry(
    draft: WalledGardenEntryDraft, *, key: str
) -> WalledGardenAllowedResource:
    try:
        return WalledGardenAllowedResource.model_validate(
            {
                "key": key,
                "label": draft.label,
                "kind": draft.kind,
                "hosts": list(draft.hosts),
                "enabled": draft.enabled,
            }
        )
    except ValidationError as exc:
        field_errors: dict[WalledGardenEntryField, str] = {}
        for error in exc.errors():
            location = error.get("loc") or ()
            name = str(location[0]) if location else ""
            try:
                entry_field = WalledGardenEntryField(name)
            except ValueError:
                entry_field = WalledGardenEntryField.label
            field_errors.setdefault(entry_field, _field_message(str(error["msg"])))
        raise WalledGardenEditError(
            WalledGardenEditErrorCode.INVALID_ENTRY,
            "The resource was not saved. Correct the highlighted fields.",
            field_errors=field_errors,
        ) from exc


def _require_current(db: Session) -> WalledGardenAllowedResources:
    resources, error = _current_resources(db)
    if resources is None:
        raise WalledGardenEditError(
            WalledGardenEditErrorCode.STORED_VALUE_INVALID,
            error or "The stored allowed-resource value is invalid.",
        )
    return resources


def _require_entry(
    resources: WalledGardenAllowedResources, key: str
) -> WalledGardenAllowedResource:
    entry = next((item for item in resources.entries if item.key == key), None)
    if entry is None:
        raise WalledGardenEditError(
            WalledGardenEditErrorCode.ENTRY_NOT_FOUND,
            "That resource no longer exists. Reload the page.",
        )
    return entry


def _submit(
    db: Session,
    *,
    context: CommandContext,
    expected_fingerprint: str,
    entries: Sequence[WalledGardenAllowedResource],
) -> tuple[str, ...]:
    try:
        value = WalledGardenAllowedResources(entries=tuple(entries))
    except ValidationError as exc:
        raise WalledGardenEditError(
            WalledGardenEditErrorCode.INVALID_ENTRY,
            f"The resource list is invalid: {_field_message(exc.errors()[0]['msg'])}",
        ) from exc
    try:
        payload = _normalize_spec_setting(
            SETTING_DOMAIN,
            SETTING_KEY,
            DomainSettingUpdate(
                value_type=SettingValueType.json,
                value_json=value.model_dump(mode="json"),
                is_active=True,
            ),
        )
    except SettingNormalizationError as exc:
        raise WalledGardenEditError(
            WalledGardenEditErrorCode.SAVE_REFUSED, exc.message
        ) from exc
    db_session_adapter.db_session_adapter.release_read_transaction(db)
    try:
        outcome = domain_settings.apply_admin_settings_form_updates(
            db,
            domain_settings.ApplyAdminSettingsFormCommand(
                context=context,
                updates=(
                    domain_settings.AdminSettingWrite(
                        domain=SETTING_DOMAIN,
                        key=SETTING_KEY,
                        payload=payload,
                        expected_value_fingerprint=expected_fingerprint,
                    ),
                ),
            ),
        )
    except domain_settings.AdminSettingsFormUpdateError as exc:
        code = (
            WalledGardenEditErrorCode.STALE
            if exc.code.endswith(".stale_update")
            else WalledGardenEditErrorCode.SAVE_REFUSED
        )
        raise WalledGardenEditError(code, exc.message) from exc
    return outcome.updated_keys


def save_walled_garden_entry(
    db: Session, *, command: SaveWalledGardenEntryCommand
) -> WalledGardenEditOutcome:
    """Add a new entry or change an existing entry's label, kind, and hosts."""

    resources = _require_current(db)
    draft = command.draft
    if command.original_key is None:
        key = draft.key.strip() or suggested_entry_key(draft.label)
        entry = _validated_entry(draft, key=key)
        if any(item.key == entry.key for item in resources.entries):
            raise WalledGardenEditError(
                WalledGardenEditErrorCode.INVALID_ENTRY,
                "The resource was not saved. Correct the highlighted fields.",
                field_errors={
                    WalledGardenEntryField.key: (
                        f"A resource with key '{entry.key}' already exists."
                    )
                },
            )
        entries = (*resources.entries, entry)
        action = WalledGardenEditAction.added
    else:
        existing = _require_entry(resources, command.original_key)
        entry = _validated_entry(
            WalledGardenEntryDraft(
                label=draft.label,
                kind=draft.kind,
                hosts=draft.hosts,
                key=existing.key,
                enabled=existing.enabled,
            ),
            key=existing.key,
        )
        entries = tuple(
            entry if item.key == existing.key else item for item in resources.entries
        )
        action = WalledGardenEditAction.updated
    updated = _submit(
        db,
        context=command.context,
        expected_fingerprint=command.expected_fingerprint,
        entries=entries,
    )
    return WalledGardenEditOutcome(
        action=action, entry_key=entry.key, updated_keys=updated
    )


def set_walled_garden_entry_enabled(
    db: Session, *, command: SetWalledGardenEntryEnabledCommand
) -> WalledGardenEditOutcome:
    """Enable or disable one entry; other entries are untouched."""

    resources = _require_current(db)
    existing = _require_entry(resources, command.key)
    changed = existing.model_copy(update={"enabled": command.enabled})
    updated = _submit(
        db,
        context=command.context,
        expected_fingerprint=command.expected_fingerprint,
        entries=tuple(
            changed if item.key == existing.key else item for item in resources.entries
        ),
    )
    return WalledGardenEditOutcome(
        action=(
            WalledGardenEditAction.enabled
            if command.enabled
            else WalledGardenEditAction.disabled
        ),
        entry_key=existing.key,
        updated_keys=updated,
    )


def remove_walled_garden_entry(
    db: Session, *, command: RemoveWalledGardenEntryCommand
) -> WalledGardenEditOutcome:
    """Remove one entry from the setting (routers keep it until re-applied)."""

    resources = _require_current(db)
    existing = _require_entry(resources, command.key)
    updated = _submit(
        db,
        context=command.context,
        expected_fingerprint=command.expected_fingerprint,
        entries=tuple(item for item in resources.entries if item.key != existing.key),
    )
    return WalledGardenEditOutcome(
        action=WalledGardenEditAction.removed,
        entry_key=existing.key,
        updated_keys=updated,
    )


__all__ = [
    "EDITABLE_KINDS",
    "RemoveWalledGardenEntryCommand",
    "SaveWalledGardenEntryCommand",
    "SetWalledGardenEntryEnabledCommand",
    "WalledGardenEditAction",
    "WalledGardenEditError",
    "WalledGardenEditErrorCode",
    "WalledGardenEditOutcome",
    "WalledGardenEntryDraft",
    "WalledGardenEntryField",
    "WalledGardenEntryFormState",
    "WalledGardenEntryRow",
    "WalledGardenFleetSummary",
    "WalledGardenFormMode",
    "WalledGardenPortalState",
    "WalledGardenRouterReadinessRow",
    "WalledGardenSettingsPage",
    "build_fleet_summary",
    "build_walled_garden_settings_page",
    "parse_hosts_text",
    "remove_walled_garden_entry",
    "save_walled_garden_entry",
    "set_walled_garden_entry_enabled",
    "suggested_entry_key",
]
