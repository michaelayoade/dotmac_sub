"""Re-encrypt legacy plaintext ``IntegrationHook.auth_config`` secret values.

Hook secrets (token/password/secret/api_key/...) are now encrypted at rest on
write and decrypted on use. This backfills rows written before the change. The
write-path encryptor is idempotent (already-``enc:`` values are left as-is), so
re-running is safe; a Fernet key rotation also encrypts any remaining plaintext.

Dry-run by default; nothing is written without --apply.

Examples
--------
  python -m scripts.one_off.encrypt_integration_hook_auth_config
  python -m scripts.one_off.encrypt_integration_hook_auth_config --apply
"""

from __future__ import annotations

import argparse

from app.db import SessionLocal
from app.models.integration_hook import SECRET_AUTH_CONFIG_KEYS, IntegrationHook
from app.services.integration_hooks import _encrypt_auth_config


def _has_plaintext_secret(auth_config) -> bool:
    if not isinstance(auth_config, dict):
        return False
    for key in SECRET_AUTH_CONFIG_KEYS:
        value = auth_config.get(key)
        if (
            value is not None
            and str(value)
            and not str(value).startswith(("enc:", "plain:"))
        ):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist changes")
    args = parser.parse_args()

    db = SessionLocal()
    legacy = 0
    total = 0
    try:
        for hook in db.query(IntegrationHook).all():
            total += 1
            if not _has_plaintext_secret(hook.auth_config):
                continue
            legacy += 1
            print(f"  legacy plaintext secret(s): {hook.id} ({hook.title})")
            if args.apply:
                hook.auth_config = _encrypt_auth_config(hook.auth_config)
        if args.apply:
            db.commit()
            print(f"Re-encrypted {legacy} of {total} hook(s).")
        else:
            print(
                f"DRY-RUN: {legacy} of {total} hook(s) would be re-encrypted. "
                "Re-run with --apply to persist."
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
