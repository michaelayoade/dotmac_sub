"""Shared notification channel policy resolution."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.notification import NotificationChannel
from app.services import settings_spec

logger = logging.getLogger(__name__)


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


def _legacy_event_channels(
    db: Session,
    *,
    template_code: str | None,
) -> tuple[NotificationChannel, ...]:
    if not template_code:
        return ()
    value = settings_spec.read_stored_value(
        db,
        SettingDomain.notification,
        f"notification_event_{template_code}_channels",
    )
    if value is None:
        return ()
    return parse_channel_list(value)


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
