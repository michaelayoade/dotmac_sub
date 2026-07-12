from __future__ import annotations

import pathlib
import re

from app.models.domain_settings import SettingDomain
from app.services import settings_spec

_DIRECT_MODEL_IO = re.compile(
    r"query\(DomainSetting\)|select\(DomainSetting|"
    r"db\.get\(DomainSetting|DomainSetting\("
)
_MODEL_IO_OWNERS = {
    "app/models/domain_settings.py",
    "app/services/credential_key_rotation.py",
    "app/services/domain_settings.py",
    "app/services/settings_health.py",
    "app/services/settings_secret_cleanup.py",
    "app/services/settings_seed.py",
    "app/services/settings_spec.py",
}
_PROCESS_ENV_EXCEPTIONS = {
    ("app/celery_app.py", "CELERY_BEAT_REFRESH_SECONDS"),
    ("app/celery_app.py", "CELERY_BROKER_URL"),
    ("app/celery_app.py", "CELERY_RESULT_BACKEND"),
    ("app/celery_app.py", "CELERY_TIMEZONE"),
    ("app/services/credential_rotation_schedule.py", "CREDENTIAL_ENCRYPTION_KEY"),
}
_ENV_READ = re.compile(r'os\.getenv\(["\']([A-Z0-9_]+)["\']')


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def test_every_setting_domain_has_one_service_owner() -> None:
    assert set(settings_spec.DOMAIN_SETTINGS_SERVICE) == set(SettingDomain)


def test_setting_registry_has_unique_environment_owners() -> None:
    owners: dict[str, tuple[SettingDomain, str]] = {}
    duplicates: list[str] = []
    for spec in settings_spec.SETTINGS_SPECS:
        if not spec.env_var:
            continue
        if spec.env_var in owners:
            duplicates.append(spec.env_var)
        owners[spec.env_var] = (spec.domain, spec.key)
    assert not duplicates, f"Environment variables with multiple owners: {duplicates}"


def test_domain_setting_io_stays_inside_control_plane_services() -> None:
    root = _repo_root()
    violations: list[str] = []
    for path in root.glob("app/**/*.py"):
        relative = str(path.relative_to(root))
        if relative in _MODEL_IO_OWNERS:
            continue
        if _DIRECT_MODEL_IO.search(path.read_text(encoding="utf-8")):
            violations.append(relative)
    assert not violations, (
        "Raw DomainSetting I/O bypasses the settings control plane: "
        f"{sorted(violations)}"
    )


def test_registered_environment_reads_are_not_duplicated_in_consumers() -> None:
    root = _repo_root()
    registered = {
        spec.env_var for spec in settings_spec.SETTINGS_SPECS if spec.env_var
    }
    excluded = {
        "app/services/settings_seed.py",
        "app/services/settings_spec.py",
    }
    violations: list[str] = []
    for path in root.glob("app/**/*.py"):
        relative = str(path.relative_to(root))
        if relative in excluded:
            continue
        for env_var in _ENV_READ.findall(path.read_text(encoding="utf-8")):
            if env_var not in registered:
                continue
            if (relative, env_var) in _PROCESS_ENV_EXCEPTIONS:
                continue
            violations.append(f"{relative}:{env_var}")
    assert not violations, (
        "Registered runtime settings have duplicate direct environment readers: "
        f"{sorted(violations)}"
    )
