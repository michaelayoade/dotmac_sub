"""Custom SQLAlchemy column types.

``EncryptedJSON`` stores a JSON dict encrypted at rest. The Python value is a
plain ``dict`` (transparent to all ORM consumers); the database stores a single
encryption-at-rest blob (``enc:``/``plain:`` per ``credential_crypto``) as plain
text. Use it for columns that hold third-party credentials (e.g. connector
``auth_config``) so a DB read / backup / replica never exposes cleartext secrets.

The blob is stored in a TEXT column (not JSON) so the raw value is a plain string
on every dialect — which lets ``credential_key_rotation`` re-encrypt it with a new
Fernet key via straight SQL, exactly like the other at-rest credential fields.
"""

from __future__ import annotations

import json

from sqlalchemy.types import Text, TypeDecorator


class EncryptedJSON(TypeDecorator):
    """A TEXT column holding a JSON dict, encrypted at rest.

    - write: ``dict`` → ``json.dumps`` → ``encrypt_credential`` → stored string.
    - read: stored string → ``decrypt_credential`` → ``json.loads`` → ``dict``.
    - back-compat: a legacy plaintext value (a JSON object as text, or NULL) is
      decoded transparently, so existing data keeps working until the one-off
      re-encrypt runs.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if not value:  # None or empty dict
            return None
        if not isinstance(value, dict):
            # Defensive: persist unexpected values without corrupting them.
            return value if isinstance(value, str) else json.dumps(value)
        from app.services.credential_crypto import encrypt_credential

        return encrypt_credential(json.dumps(value, sort_keys=True))

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        # Legacy rows (pre-migration JSON column) may surface as a dict.
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
