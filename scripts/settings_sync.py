import argparse
import os

from dotenv import load_dotenv

from app.db import SessionLocal
from app.schemas.settings import DomainSettingUpdate
from app.services.settings_spec import (
    DOMAIN_SETTINGS_SERVICE,
    SETTINGS_SPECS,
    coerce_value,
    normalize_for_db,
)
from app.services.secrets import is_openbao_ref


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sync settings from env to DB (one-way)."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-plaintext", action="store_true")
    return parser.parse_args()


def main():
    load_dotenv()
    args = parse_args()
    db = SessionLocal()
    updated = 0
    skipped = 0
    errors: list[str] = []
    try:
        for spec in SETTINGS_SPECS:
            if not spec.env_var:
                skipped += 1
                continue
            env_raw = _env_value(spec.env_var)
            if env_raw is None:
                skipped += 1
                continue
            if spec.is_secret and not is_openbao_ref(env_raw) and not args.allow_plaintext:
                errors.append(
                    f"{spec.domain.value}.{spec.key}: secret must be an OpenBao reference (or use --allow-plaintext)"
                )
                continue
            value, error = coerce_value(spec, env_raw)
            if error:
                errors.append(f"{spec.domain.value}.{spec.key}: env {error}")
                continue
            value_text, value_json = normalize_for_db(spec, value)
            payload = DomainSettingUpdate(
                value_type=spec.value_type,
                value_text=value_text,
                value_json=value_json,
                is_secret=spec.is_secret,
                is_active=True,
            )
            if args.dry_run:
                print(
                    f"dry-run: {spec.domain.value}.{spec.key} <= {spec.env_var}={env_raw}"
                )
                updated += 1
                continue
            service = DOMAIN_SETTINGS_SERVICE.get(spec.domain)
            if not service:
                errors.append(f"{spec.domain.value}.{spec.key}: no domain service")
                continue
            service.upsert_by_key(db, spec.key, payload)
            updated += 1
    finally:
        db.close()

    if errors:
        print("Settings sync failed:")
        for item in errors:
            print(f"- {item}")
        raise SystemExit(1)
    print(f"Settings sync complete. updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
