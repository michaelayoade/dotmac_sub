"""Runtime integrity inspection for the settings control plane."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.services import settings_spec
from app.services.secrets import is_openbao_ref
from app.services.settings_cache import SettingsCache
from app.services.settings_spec import RETIRED_SETTING_KEYS, coerce_value, get_spec

_DYNAMIC_KEY_PREFIXES: dict[SettingDomain, tuple[str, ...]] = {
    SettingDomain.imports: ("export_job.", "export_template."),
    SettingDomain.notification: (
        "notification_event_",
        "smtp_activity_sender.",
        "smtp_sender.",
    ),
}
_DYNAMIC_KEYS: dict[SettingDomain, set[str]] = {
    SettingDomain.radius: {
        "device_login_last_sync",
        "reject_ip_initial_push_done_at",
        "reject_ip_runtime_state",
    },
}


@dataclass(frozen=True)
class SettingsHealthReport:
    registered: int
    active_rows: int
    inactive_registered: int
    retired_active: tuple[str, ...] = field(default_factory=tuple)
    unknown_active: tuple[str, ...] = field(default_factory=tuple)
    invalid_active: tuple[str, ...] = field(default_factory=tuple)
    secret_mismatches: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not (
            self.retired_active
            or self.unknown_active
            or self.invalid_active
            or self.secret_mismatches
        )


def _is_dynamic_key(domain: SettingDomain, key: str) -> bool:
    if key in _DYNAMIC_KEYS.get(domain, set()):
        return True
    return key.startswith(_DYNAMIC_KEY_PREFIXES.get(domain, ()))


def inspect_settings(db: Session) -> SettingsHealthReport:
    rows = db.query(DomainSetting).all()
    unknown: list[str] = []
    retired: list[str] = []
    invalid: list[str] = []
    secret_mismatches: list[str] = []
    inactive_registered = 0

    for row in rows:
        spec = get_spec(row.domain, row.key)
        identity = f"{row.domain.value}.{row.key}"
        if spec is None:
            if row.is_active and row.key in RETIRED_SETTING_KEYS:
                retired.append(identity)
            elif row.is_active and not _is_dynamic_key(row.domain, row.key):
                unknown.append(identity)
            continue
        if not row.is_active:
            inactive_registered += 1
            continue
        raw = row.value_text if row.value_text is not None else row.value_json
        _value, error = coerce_value(spec, raw)
        if error or row.value_type != spec.value_type:
            invalid.append(identity)
        if spec.is_secret and (
            not row.is_secret
            or not isinstance(row.value_text, str)
            or not is_openbao_ref(row.value_text)
        ):
            secret_mismatches.append(identity)

    return SettingsHealthReport(
        registered=len(settings_spec.SETTINGS_SPECS),
        active_rows=sum(1 for row in rows if row.is_active),
        inactive_registered=inactive_registered,
        retired_active=tuple(sorted(retired)),
        unknown_active=tuple(sorted(unknown)),
        invalid_active=tuple(sorted(invalid)),
        secret_mismatches=tuple(sorted(secret_mismatches)),
    )


def deactivate_retired_settings(db: Session, *, apply: bool = False) -> tuple[str, ...]:
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.key.in_(RETIRED_SETTING_KEYS))
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    identities = tuple(sorted(f"{row.domain.value}.{row.key}" for row in rows))
    if not apply or not rows:
        return identities

    domains = {row.domain for row in rows}
    for row in rows:
        row.is_active = False
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    for domain in domains:
        SettingsCache.invalidate_domain(domain.value)
    return identities
