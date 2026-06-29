"""Custom SQLAlchemy column types.

``EncryptedJSON`` stores a JSON dict encrypted at rest. The Python value is a
plain ``dict`` (transparent to all ORM consumers); the database stores a single
encryption-at-rest blob (``enc:``/``plain:`` per ``credential_crypto``) as a JSON
string. Use it for columns that hold third-party credentials (e.g. connector
``auth_config``) so a DB read / backup / replica never exposes cleartext secrets.
"""

from __future__ import annotations

import json

from sqlalchemy.types import JSON, TypeDecorator


class EncryptedJSON(TypeDecorator):
    """A JSON column whose payload is encrypted at rest.

    - write: ``dict`` → ``json.dumps`` → ``encrypt_credential`` → stored string.
    - read: stored string → ``decrypt_credential`` → ``json.loads`` → ``dict``.
    - back-compat: a legacy plaintext row (stored as a JSON object, i.e. a
      ``dict``, or ``NULL``) is returned unchanged, so existing data keeps working
      until the one-off re-encrypt runs. The DDL is unchanged (``impl = JSON``),
      so no migration is required.
    """

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        # Only encrypt dict payloads; leave anything unexpected untouched.
        if not isinstance(value, dict):
            return value
        if not value:
            return {}
        from app.services.credential_crypto import encrypt_credential

        return encrypt_credential(json.dumps(value, sort_keys=True))

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        # Legacy plaintext rows were stored as a JSON object.
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            if not value:
                return None
            from app.services.credential_crypto import decrypt_credential

            candidates = []
            try:
                candidates.append(decrypt_credential(value))
            except Exception:
                pass
            candidates.append(value)  # fall back to treating it as plain JSON
            for candidate in candidates:
                if not candidate:
                    continue
                try:
                    parsed = json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    return parsed
            return None
        return value
