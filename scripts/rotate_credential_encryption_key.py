#!/usr/bin/env python3
"""Rotate the credential-at-rest Fernet key and rewrite stored values."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.services.credential_crypto import generate_encryption_key, get_encryption_key
from app.services.credential_key_rotation import (
    rotate_credential_encryption_material,
    update_openbao_credential_encryption_key,
)


def _emit_json(payload: dict[str, object], *, stream=sys.stdout) -> None:
    print(json.dumps(payload), file=stream)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rotate the credential encryption key and re-encrypt stored values."
    )
    parser.add_argument(
        "--new-key",
        help="New Fernet key. If omitted with --generate, one will be generated.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate a new Fernet key when --new-key is omitted.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the rotation instead of running a dry run.",
    )
    parser.add_argument(
        "--update-openbao",
        action="store_true",
        help="After data rotation, write the new key to OpenBao settings/auth.",
    )
    parser.add_argument(
        "--print-key",
        action="store_true",
        help="Include the new key in stdout output. Use with care because this exposes secret material.",
    )
    args = parser.parse_args()

    old_key = get_encryption_key()
    if not old_key:
        _emit_json(
            {
                "ok": False,
                "error": "Current credential encryption key is not configured.",
            },
            stream=sys.stderr,
        )
        return 1

    new_key = args.new_key or (generate_encryption_key() if args.generate else None)
    if not new_key:
        _emit_json(
            {"ok": False, "error": "Provide --new-key or use --generate."},
            stream=sys.stderr,
        )
        return 1

    if isinstance(old_key, bytes):
        old_key_text = old_key.decode("ascii")
    else:
        old_key_text = str(old_key)

    session = SessionLocal()
    try:
        try:
            result = rotate_credential_encryption_material(
                session,
                old_key=old_key_text,
                new_key=new_key,
                commit=args.apply,
            )
            if not args.apply:
                session.rollback()
        except Exception as exc:
            session.rollback()
            error_payload: dict[str, object] = {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "apply": args.apply,
            }
            if args.print_key:
                error_payload["new_key"] = new_key
            _emit_json(error_payload, stream=sys.stderr)
            return 2

        payload: dict[str, object] = {
            "ok": True,
            "apply": args.apply,
            "updated_records": result.updated_records,
            "updated_values": result.updated_values,
            "key_printed": args.print_key,
        }
        if args.print_key:
            payload["new_key"] = new_key
            payload["warning"] = (
                "The credential encryption key is included in stdout output."
            )
        _emit_json(payload)
        if not args.print_key:
            _emit_json(
                {
                    "ok": True,
                    "warning": (
                        "Credential encryption key not printed. "
                        "Re-run with --print-key if you need it in stdout."
                    ),
                },
                stream=sys.stderr,
            )
        if args.apply and args.update_openbao:
            success = update_openbao_credential_encryption_key(new_key)
            _emit_json({"ok": success, "openbao_updated": success})
            if not success:
                return 3
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
