"""Encrypt legacy OAuth tokens and connector headers after migration 262.

Dry-run by default. Run with ``--apply`` after deploying the migration while
``CREDENTIAL_ENCRYPTION_KEY`` is configured. The operation is idempotent.
"""

from __future__ import annotations

import argparse

from sqlalchemy import text

from app.db import SessionLocal
from app.models.connector import ConnectorConfig
from app.models.oauth_token import OAuthToken
from app.services.credential_crypto import get_encryption_key
from app.services.secrets import is_secret_ref


def _needs_encryption(value: object) -> bool:
    if value is None:
        return False
    raw = str(value)
    return bool(raw) and not raw.startswith("enc:") and not is_secret_ref(raw)


def run(*, apply: bool) -> dict[str, int]:
    if apply and not get_encryption_key():
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is required for --apply")

    counts = {"oauth_values": 0, "connector_headers": 0}
    with SessionLocal() as db:
        oauth_rows = db.execute(
            text("SELECT id, access_token, refresh_token FROM oauth_tokens")
        ).mappings()
        for row in oauth_rows:
            fields = [
                name
                for name in ("access_token", "refresh_token")
                if _needs_encryption(row[name])
            ]
            counts["oauth_values"] += len(fields)
            if apply and fields:
                token = db.get(OAuthToken, row["id"])
                if token is not None:
                    for field in fields:
                        setattr(token, field, getattr(token, field))

        connector_rows = db.execute(
            text("SELECT id, headers FROM connector_configs WHERE headers IS NOT NULL")
        ).mappings()
        for row in connector_rows:
            if not _needs_encryption(row["headers"]):
                continue
            counts["connector_headers"] += 1
            if apply:
                connector = db.get(ConnectorConfig, row["id"])
                if connector is not None:
                    connector.headers = dict(connector.headers or {})

        if apply:
            db.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    counts = run(apply=args.apply)
    mode = "encrypted" if args.apply else "would encrypt"
    print(f"{mode}: {counts['oauth_values']} OAuth values")
    print(f"{mode}: {counts['connector_headers']} connector header blobs")


if __name__ == "__main__":
    main()
