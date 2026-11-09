"""Helpers for processing admin system settings form submissions."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import db_session_adapter, domain_settings, settings_spec
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.settings_api_custom import (
    SettingNormalizationError,
    _normalize_spec_setting,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HubSettingsUpdateCommand:
    section_id: str
    values: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HubSettingsUpdateResult:
    errors: tuple[str, ...]


def update_hub_section(
    db: Session,
    *,
    command: HubSettingsUpdateCommand,
    context: CommandContext,
) -> HubSettingsUpdateResult:
    """Only the section's declared identities participate in this atomic save."""
    from app.services.web_system_settings_hub import HubSectionQuery, section_specs

    if command.section_id in {"branding-values", "company"}:
        return HubSettingsUpdateResult(
            errors=("Use this section's specialised editor.",)
        )
    specs = section_specs(query=HubSectionQuery(section_id=command.section_id))
    permitted = {spec.key for spec in specs} | {"_csrf_token"}
    if set(command.values) - permitted:
        return HubSettingsUpdateResult(
            errors=("The submission contains fields outside this section.",)
        )
    return update_registered_settings(
        db,
        command=RegisteredSettingsUpdateCommand(specs=specs, values=command.values),
        context=context,
    )


@dataclass(frozen=True, slots=True)
class RegisteredSettingsUpdateCommand:
    specs: tuple[settings_spec.SettingSpec, ...]
    values: Mapping[str, object]


def update_registered_settings(
    db: Session,
    *,
    command: RegisteredSettingsUpdateCommand,
    context: CommandContext,
) -> HubSettingsUpdateResult:
    """One save path for friendly and generic forms; inherited values stay inherited."""
    updates, errors = _prepare_settings_from_specs(
        form=command.values,
        specs=tuple(spec for spec in command.specs if spec.key in command.values),
    )
    if errors:
        return HubSettingsUpdateResult(errors=errors)
    changed: list[domain_settings.AdminSettingWrite] = []
    for update in updates:
        spec = settings_spec.get_spec(update.domain, update.key)
        if spec is None:
            return HubSettingsUpdateResult(
                errors=("A submitted setting is no longer registered.",)
            )
        if not spec.is_secret:
            desired, error = settings_spec.coerce_value(
                spec,
                update.payload.value_text
                if update.payload.value_text is not None
                else update.payload.value_json,
            )
            if error:
                return HubSettingsUpdateResult(errors=(f"{spec.key}: {error}",))
            current, _source = settings_spec.resolve_with_source(
                db, spec.domain, spec.key
            )
            if desired == current:
                continue
        changed.append(update)
    return HubSettingsUpdateResult(
        errors=_apply_prepared_settings(db=db, context=context, updates=tuple(changed))
    )


def safe_submitted_values(
    *,
    specs: Sequence[settings_spec.SettingSpec],
    values: Mapping[str, object],
) -> dict[str, object]:
    """Keep attempted non-secret fields for error rendering, never passwords or tokens."""
    return {
        spec.key: (
            form_bool(values.get(spec.key))
            if spec.value_type == SettingValueType.boolean
            else values.get(spec.key, "")
        )
        for spec in specs
        if spec.key in values and not spec.is_secret and not spec.required_permission
    }


def form_bool(value: object | None) -> bool:
    """Parse common HTML form boolean values."""
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_blank(value: object | None) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _raw_update_payload(
    spec: settings_spec.SettingSpec,
    value: object,
) -> DomainSettingUpdate:
    if isinstance(value, dict | list):
        return DomainSettingUpdate(
            value_type=spec.value_type,
            value_json=value,
            is_secret=spec.is_secret,
            is_active=True,
        )
    return DomainSettingUpdate(
        value_text=str(value),
        is_secret=spec.is_secret,
        is_active=True,
    )


def _prepare_settings_from_specs(
    *,
    form: Mapping[str, object],
    specs: Sequence[settings_spec.SettingSpec],
    include_permission_gated: bool = False,
) -> tuple[tuple[domain_settings.AdminSettingWrite, ...], tuple[str, ...]]:
    updates: list[domain_settings.AdminSettingWrite] = []
    errors: list[str] = []

    for spec in specs:
        # A setting that declares ``required_permission`` is never part of a
        # generic tab save: a missing checkbox would otherwise write False and
        # either be refused for everyone or silently rewrite the switch. It is
        # changed only through its dedicated form (``include_permission_gated``).
        if spec.required_permission and not include_permission_gated:
            continue
        raw = form.get(spec.key)
        blank = _is_blank(raw)
        if spec.is_secret and blank:
            continue

        if spec.value_type == SettingValueType.boolean:
            value: object = form_bool(raw)
        elif blank:
            if spec.default is None:
                if spec.required:
                    errors.append(f"{spec.key}: Value is required.")
                # Optional settings without defaults are represented by no row,
                # not by an invalid typed row with no value.
                continue
            value = spec.default
        else:
            value = raw

        try:
            payload = _normalize_spec_setting(
                spec.domain,
                spec.key,
                _raw_update_payload(spec, value),
            )
        except SettingNormalizationError as exc:
            errors.append(f"{spec.key}: {exc.message}")
            continue
        except (TypeError, ValueError):
            errors.append(f"{spec.key}: Invalid setting value.")
            continue

        updates.append(
            domain_settings.AdminSettingWrite(
                domain=spec.domain,
                key=spec.key,
                payload=payload,
            )
        )

    return tuple(updates), tuple(errors)


def _apply_prepared_settings(
    *,
    db: Session,
    context: CommandContext,
    updates: tuple[domain_settings.AdminSettingWrite, ...],
) -> tuple[str, ...]:
    if not updates:
        return ()
    try:
        db_session_adapter.db_session_adapter.release_read_transaction(db)
        domain_settings.apply_admin_settings_form_updates(
            db,
            domain_settings.ApplyAdminSettingsFormCommand(
                context=context,
                updates=updates,
            ),
        )
    except DomainError as exc:
        return (exc.message,)
    except Exception as exc:
        logger.error(
            "Admin settings form update failed",
            extra={"error_type": type(exc).__name__},
        )
        return ("Settings could not be saved. No changes were made.",)
    return ()


def upsert_settings_from_specs(
    *,
    db: Session,
    form: Mapping[str, object],
    specs: Sequence[settings_spec.SettingSpec],
    service: domain_settings.DomainSettings,
    context: CommandContext,
    skip_blank_secrets: bool = True,
) -> list[str]:
    """Validate the complete submission, then save it in one transaction."""
    if not skip_blank_secrets:
        raise ValueError("Admin setting forms must preserve blank secrets.")
    if service.domain is None or any(spec.domain != service.domain for spec in specs):
        return ["Settings service does not match the submitted domain."]
    updates, errors = _prepare_settings_from_specs(form=form, specs=specs)
    if errors:
        return list(errors)
    return list(_apply_prepared_settings(db=db, context=context, updates=updates))


_STARTER_LEDGER_LIVE_KEY = "starter_ledger_live_enabled"


def set_starter_ledger_live_switch(
    *,
    db: Session,
    enabled: bool,
    context: CommandContext,
) -> tuple[str, ...]:
    """Dedicated write of the D1 switch; returns error messages (empty on success).

    The owner command enforces ``billing:starter_ledger_live:write`` (strict
    grant, refusals audited) before and inside its transaction.
    """
    spec = settings_spec.get_spec(
        settings_spec.SettingDomain.billing, _STARTER_LEDGER_LIVE_KEY
    )
    if spec is None:
        return ("Setting is not registered.",)
    updates, errors = _prepare_settings_from_specs(
        form={spec.key: "true" if enabled else ""},
        specs=[spec],
        include_permission_gated=True,
    )
    if errors:
        return errors
    return _apply_prepared_settings(db=db, context=context, updates=updates)
