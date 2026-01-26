import os

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.domain_settings import DomainSetting
from app.services.settings_spec import SETTINGS_SPECS, coerce_value, extract_db_value
from app.services.secrets import is_openbao_ref


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def main():
    load_dotenv()
    db = SessionLocal()
    try:
        rows = (
            db.query(DomainSetting)
            .filter(DomainSetting.is_active.is_(True))
            .all()
        )
        db_map = {(row.domain, row.key): row for row in rows}
        errors: list[str] = []
        for spec in SETTINGS_SPECS:
            env_raw = _env_value(spec.env_var) if spec.env_var else None
            env_value, env_error = coerce_value(spec, env_raw) if env_raw is not None else (None, None)
            if env_error:
                errors.append(f"{spec.domain.value}.{spec.key}: env {env_error}")
                continue
            db_setting = db_map.get((spec.domain, spec.key))
            db_raw = extract_db_value(db_setting)
            if spec.is_secret and db_raw:
                if isinstance(db_raw, str) and not is_openbao_ref(db_raw):
                    errors.append(
                        f"{spec.domain.value}.{spec.key}: secret must be an OpenBao reference"
                    )
                    continue
            db_value, db_error = coerce_value(spec, db_raw) if db_raw is not None else (None, None)
            if db_error:
                errors.append(f"{spec.domain.value}.{spec.key}: db {db_error}")
                continue
            effective = env_value if env_raw is not None else db_value
            if effective is None:
                effective = spec.default
            if spec.required and effective is None:
                errors.append(f"{spec.domain.value}.{spec.key}: required value missing")
                continue
            if effective is None:
                continue
            if spec.allowed:
                normalized = str(effective).lower()
                if normalized not in {item.lower() for item in spec.allowed}:
                    errors.append(
                        f"{spec.domain.value}.{spec.key}: value must be one of {sorted(spec.allowed)}"
                    )
            if spec.min_value is not None:
                try:
                    if int(effective) < spec.min_value:
                        errors.append(
                            f"{spec.domain.value}.{spec.key}: value must be >= {spec.min_value}"
                        )
                except (TypeError, ValueError):
                    errors.append(f"{spec.domain.value}.{spec.key}: value must be an integer")
            if spec.max_value is not None:
                try:
                    if int(effective) > spec.max_value:
                        errors.append(
                            f"{spec.domain.value}.{spec.key}: value must be <= {spec.max_value}"
                        )
                except (TypeError, ValueError):
                    errors.append(f"{spec.domain.value}.{spec.key}: value must be an integer")
    finally:
        db.close()

    if errors:
        print("Settings validation failed:")
        for item in errors:
            print(f"- {item}")
        raise SystemExit(1)
    print("Settings validation passed.")


if __name__ == "__main__":
    main()
