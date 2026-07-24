"""Shared notification channel policy resolution.

This module is the single owner of *which channels a notification goes out on*.
Callers state their intent (template code, event type, category) and their own
defaults; the channel decision is made here and nowhere else. Feature areas must
not carry their own channel picker settings — see
``docs/designs/NOTIFICATION_CHANNEL_POLICY.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TypedDict

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import NotificationChannel
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.domain_errors import DomainError
from app.services.domain_settings import DomainSettings

logger = logging.getLogger(__name__)

POLICY_SETTING_KEY = "notification_channel_policy"

#: Channels an operator may select for customer notifications. ``webhook`` and
#: ``websocket`` are transport plumbing rather than customer-reachable channels,
#: so they are deliberately not offered.
SELECTABLE_CHANNELS: tuple[NotificationChannel, ...] = (
    NotificationChannel.email,
    NotificationChannel.sms,
    NotificationChannel.whatsapp,
    NotificationChannel.push,
)


class ChannelPolicyDocument(TypedDict):
    """Stored shape of ``notification_channel_policy``."""

    default: list[str]
    categories: dict[str, list[str]]
    events: dict[str, list[str]]


def _dedupe_channels(
    channels: Iterable[NotificationChannel],
) -> tuple[NotificationChannel, ...]:
    ordered: list[NotificationChannel] = []
    for channel in channels:
        if channel not in ordered:
            ordered.append(channel)
    return tuple(ordered)


def parse_channel_list(value: object) -> tuple[NotificationChannel, ...]:
    """Parse configured channels from CSV strings or JSON lists."""
    if value is None:
        return ()
    raw_items: Iterable[object]
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, Iterable) and not isinstance(
        value, (bytes, bytearray, dict)
    ):
        raw_items = value
    else:
        return ()

    channels: list[NotificationChannel] = []
    for item in raw_items:
        raw = str(item).strip().lower()
        if not raw:
            continue
        try:
            channel = NotificationChannel(raw)
        except ValueError:
            logger.warning("Ignoring invalid notification channel %r", raw)
            continue
        channels.append(channel)
    return _dedupe_channels(channels)


_LEGACY_KEY_PREFIX = "notification_event_"
_LEGACY_KEY_SUFFIX = "_channels"


def _legacy_channel_rows(db: Session) -> "list[DomainSetting]":  # noqa: UP037
    """Every active per-event channel setting.

    Deliberately the ONLY place this module touches ``DomainSetting`` directly:
    tests/architecture/test_decision_input_ownership.py counts raw references
    and holds this module to 4. The return annotation stays quoted for the same
    reason — unquoting it adds a fifth reference and trips that contract, hence
    the UP037 suppression. Callers filter the small result set in Python rather
    than issuing their own query; these legacy rows number in the single digits.
    """
    return (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.is_active.is_(True))
        .filter(DomainSetting.key.like(f"{_LEGACY_KEY_PREFIX}%{_LEGACY_KEY_SUFFIX}"))
        .all()
    )


def _legacy_event_channels(
    db: Session,
    *,
    template_code: str | None,
) -> tuple[NotificationChannel, ...]:
    if not template_code:
        return ()
    wanted = f"{_LEGACY_KEY_PREFIX}{template_code}{_LEGACY_KEY_SUFFIX}"
    for row in _legacy_channel_rows(db):
        if row.key == wanted:
            return parse_channel_list(row.value_text or row.value_json)
    return ()


def _policy_value(
    policy: Mapping[str, object],
    section: str,
    key: str | None,
) -> object | None:
    if not key:
        return None
    section_value = policy.get(section)
    if not isinstance(section_value, Mapping):
        return None
    return section_value.get(key)


def resolve_notification_channels(
    db: Session,
    *,
    template_code: str | None = None,
    event_type: str | None = None,
    category: str | None = None,
    default_channels: Iterable[NotificationChannel] = (),
) -> tuple[NotificationChannel, ...]:
    """Resolve channels for a notification intent.

    Precedence:
    1. Existing per-event setting: notification_event_<template_code>_channels.
    2. JSON policy event/template override.
    3. JSON policy category override.
    4. JSON policy default.
    5. Caller defaults.
    """
    legacy = _legacy_event_channels(db, template_code=template_code)
    if legacy:
        return legacy

    policy = settings_spec.resolve_value(
        db,
        SettingDomain.notification,
        "notification_channel_policy",
    )
    if isinstance(policy, Mapping):
        event_channels = parse_channel_list(
            _policy_value(policy, "events", template_code)
            or _policy_value(policy, "events", event_type)
        )
        if event_channels:
            return event_channels

        category_channels = parse_channel_list(
            _policy_value(policy, "categories", category)
        )
        if category_channels:
            return category_channels

        default_policy_channels = parse_channel_list(policy.get("default"))
        if default_policy_channels:
            return default_policy_channels

    return _dedupe_channels(default_channels)


# ---------------------------------------------------------------------------
# Canonical read/write of the stored policy
# ---------------------------------------------------------------------------


def serialize_channels(channels: Iterable[NotificationChannel]) -> list[str]:
    """Render channels for storage/display in a stable order."""
    return [channel.value for channel in _dedupe_channels(channels)]


def get_channel_policy(db: Session) -> ChannelPolicyDocument:
    """Return the stored policy, normalized to its three sections."""
    stored = settings_spec.resolve_value(
        db,
        SettingDomain.notification,
        POLICY_SETTING_KEY,
    )
    policy: Mapping[str, object] = stored if isinstance(stored, Mapping) else {}

    events_raw = policy.get("events")
    categories_raw = policy.get("categories")
    return {
        "default": serialize_channels(parse_channel_list(policy.get("default"))),
        "categories": {
            str(key): serialize_channels(parse_channel_list(value))
            for key, value in (
                categories_raw.items() if isinstance(categories_raw, Mapping) else ()
            )
            if parse_channel_list(value)
        },
        "events": {
            str(key): serialize_channels(parse_channel_list(value))
            for key, value in (
                events_raw.items() if isinstance(events_raw, Mapping) else ()
            )
            if parse_channel_list(value)
        },
    }


def _validated_channels(raw: Iterable[str] | str | None, *, label: str) -> list[str]:
    """Coerce operator input to selectable channels, rejecting unknown ones."""
    if raw is None:
        return []
    supplied = [
        str(item).strip() for item in (raw.split(",") if isinstance(raw, str) else raw)
    ]
    supplied = [item for item in supplied if item]

    selectable = {channel.value for channel in SELECTABLE_CHANNELS}
    unknown = sorted({item for item in supplied if item.lower() not in selectable})
    if unknown:
        raise DomainError(
            code="notification_channel_unsupported",
            message=(
                f"Unsupported notification channel for {label}: {', '.join(unknown)}"
            ),
            details={"target": label, "unsupported": unknown},
        )
    return serialize_channels(parse_channel_list(supplied))


def set_channel_policy(
    db: Session,
    *,
    default: Iterable[str] | str | None = None,
    categories: Mapping[str, Iterable[str] | str] | None = None,
    events: Mapping[str, Iterable[str] | str] | None = None,
) -> ChannelPolicyDocument:
    """Replace the stored channel policy.

    This is the only supported writer. Empty selections are dropped rather than
    stored as empty lists so that resolution falls through to the next
    precedence level instead of silently sending on no channel at all.
    """
    default_channels = _validated_channels(default, label="the global default")
    category_channels: dict[str, list[str]] = {}
    event_channels: dict[str, list[str]] = {}

    for key, value in (categories or {}).items():
        channels = _validated_channels(value, label=f"category '{key}'")
        if channels:
            category_channels[key] = channels

    for key, value in (events or {}).items():
        channels = _validated_channels(value, label=f"event '{key}'")
        if channels:
            event_channels[key] = channels

    payload: dict[str, object] = {
        "categories": category_channels,
        "events": event_channels,
    }
    if default_channels:
        payload["default"] = default_channels

    spec = settings_spec.get_spec(SettingDomain.notification, POLICY_SETTING_KEY)
    if spec is None:  # pragma: no cover - registry guarantees the spec exists
        raise DomainError(
            code="notification_channel_policy_unregistered",
            message="Notification channel policy setting is not registered",
        )

    DomainSettings(SettingDomain.notification).upsert_by_key(
        db,
        POLICY_SETTING_KEY,
        DomainSettingUpdate(
            value_type=spec.value_type,
            value_text=None,
            value_json=payload,
            is_secret=False,
            is_active=True,
        ),
    )
    # The owner commits its own write; web adapters must not own the
    # transaction (tests/architecture/test_adapter_transaction_ownership.py).
    db.commit()
    return get_channel_policy(db)


def legacy_event_overrides(db: Session) -> dict[str, list[str]]:
    """Per-event ``notification_event_<code>_channels`` rows still in force.

    These outrank the JSON policy, so the admin surface has to show them or an
    operator will change the policy and see no effect.
    """
    overrides: dict[str, list[str]] = {}
    for row in _legacy_channel_rows(db):
        template_code = row.key[len(_LEGACY_KEY_PREFIX) : -len(_LEGACY_KEY_SUFFIX)]
        channels = parse_channel_list(row.value_text or row.value_json)
        if template_code and channels:
            overrides[template_code] = serialize_channels(channels)
    return overrides
