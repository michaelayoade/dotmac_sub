"""Canonical customer identifier helpers.

New customer-facing identifiers derive from one canonical numeric id:

* subscriber_number: ``SUB-<canonical>``
* account_number: ``ACC-<canonical>``
* PPPoE username: ``10<canonical>``

Existing imported/manual values are preserved by callers; these helpers only
format or parse values when the configured subscriber-number shape is clear.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

PPPOE_USERNAME_PREFIX = "10"


def _setting_text(db: Session, domain: SettingDomain, key: str, fallback: str) -> str:
    value = settings_spec.resolve_value(db, domain, key)
    return value if isinstance(value, str) else fallback


def subscriber_prefix(db: Session) -> str:
    return _setting_text(db, SettingDomain.subscriber, "subscriber_number_prefix", "SUB-")


def account_prefix(db: Session) -> str:
    return _setting_text(db, SettingDomain.subscriber, "account_number_prefix", "ACC-")


def canonical_id_from_subscriber_number(
    db: Session,
    subscriber_number: str | None,
) -> str | None:
    number = str(subscriber_number or "").strip()
    if not number:
        return None

    prefix = subscriber_prefix(db)
    if prefix:
        if not number.startswith(prefix):
            return None
        canonical = number[len(prefix) :]
    else:
        canonical = number

    canonical = canonical.strip()
    if not canonical or not canonical.isdigit():
        return None
    return canonical


def account_number_from_canonical(db: Session, canonical_id: str | None) -> str | None:
    if not canonical_id:
        return None
    return f"{account_prefix(db)}{canonical_id}"


def account_number_from_subscriber_number(
    db: Session,
    subscriber_number: str | None,
) -> str | None:
    return account_number_from_canonical(
        db,
        canonical_id_from_subscriber_number(db, subscriber_number),
    )


def pppoe_username_from_canonical(canonical_id: str | None) -> str | None:
    if not canonical_id:
        return None
    return f"{PPPOE_USERNAME_PREFIX}{canonical_id}"


def pppoe_username_from_subscriber_number(
    db: Session,
    subscriber_number: str | None,
) -> str | None:
    return pppoe_username_from_canonical(
        canonical_id_from_subscriber_number(db, subscriber_number),
    )
