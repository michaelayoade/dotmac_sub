"""Re-encrypt legacy plaintext ``ConnectorConfig.auth_config`` rows at rest.

The column is now an ``EncryptedJSON`` type (encrypt-at-rest). New writes are
encrypted automatically; this backfills rows written before the change, which the
type still reads transparently (back-compat) but stores as cleartext JSON.

A row is "legacy plaintext" when its raw DB value is a JSON object (dict). This
script reads each connector's ``auth_config`` through the ORM (transparently
decoded), reassigns it, and commits — which re-binds through ``EncryptedJSON`` and
writes the encrypted blob. Idempotent: already-encrypted rows re-encrypt to the
same plaintext (no semantic change).

Dry-run by default; nothing is written without --apply.

Examples
--------
  python -m scripts.one_off.encrypt_connector_auth_config
  python -m scripts.one_off.encrypt_connector_auth_config --apply
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db import SessionLocal
from app.models.connector import ConnectorConfig


def _raw_is_plaintext(db, config_id) -> bool:
    """True when the raw stored value is a JSON object (legacy plaintext)."""
    raw = db.execute(
        text("SELECT auth_config FROM connector_configs WHERE id = :id"),
        {"id": str(config_id)},
    ).scalar()
    # A legacy row decodes to a dict; an encrypted row is a JSON string blob.
    return isinstance(raw, dict)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist changes")
    args = parser.parse_args()

    db = SessionLocal()
    legacy = 0
    total = 0
    try:
        configs = db.query(ConnectorConfig).all()
        for config in configs:
            total += 1
            if not _raw_is_plaintext(db, config.id):
                continue
            legacy += 1
            print(f"  legacy plaintext auth_config: {config.id} ({config.name})")
            if args.apply:
                value = config.auth_config
                # Reassign a fresh dict so SQLAlchemy marks the attribute dirty
                # and re-binds it through EncryptedJSON on commit.
                config.auth_config = dict(value) if isinstance(value, dict) else value
        if args.apply:
            db.commit()
            print(f"Re-encrypted {legacy} of {total} connector(s).")
        else:
            print(
                f"DRY-RUN: {legacy} of {total} connector(s) would be re-encrypted. "
                "Re-run with --apply to persist."
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
