# Connector secret encryption at rest

**Date:** 2026-06-29
**Context:** the top remaining P0 from `SECURITY_REVIEW_ADMIN_AUTHZ.md` — integration
`ConnectorConfig.auth_config` (basic-auth passwords, bearer/HMAC/api_key secrets)
was a plain `JSON` column, stored unencrypted. Any DB read / backup / replica
exposed third-party credentials in cleartext, unlike NAS/router creds which are
`credential_crypto`-encrypted.

## Approach — transparent column-level encryption

A new `EncryptedJSON` SQLAlchemy `TypeDecorator` (`app/models/types.py`) encrypts the
column at rest:

- **write:** `dict` → `json.dumps` → `encrypt_credential` → stored string
  (`enc:<fernet>`; or `plain:<json>` when no key is configured).
- **read:** stored string → `decrypt_credential` → `json.loads` → `dict`.
- **back-compat:** a legacy row (stored as a JSON object / `dict`, or `NULL`) is
  returned unchanged, so existing data keeps working until the re-encrypt runs.

`ConnectorConfig.auth_config` now uses it (`app/models/connector.py`). Because the
ORM attribute is transparently the plaintext `dict`, **every in-process consumer is
unchanged** — `nextcloud_talk`, `connector_service.update`'s merge, and
`provisioning_helpers._resolve_connector_context`. Only the API schema
(`ConnectorConfigRead._mask_auth_config`) still masks, for output.

`impl = JSON`, so the DB column type is unchanged — **no Alembic migration needed**.

## Backfill

`scripts/one_off/encrypt_connector_auth_config.py` re-encrypts legacy plaintext
rows (dry-run by default; `--apply` to persist). Idempotent.

## Tests

`tests/test_connector_auth_config_encryption.py` — type round-trip, none/empty,
legacy-plaintext read-through, and a DB integration test asserting the stored value
is an `enc:`/`plain:` blob (and ciphertext omits the secret when a key is set). The
existing connector/integration/provisioning suites (~75 tests) pass unchanged,
confirming consumer transparency.

## Deferred (separate follow-ups)

- **`IntegrationHook.auth_config`** (security-review #6) is also plaintext, but it
  interacts with `credential_key_rotation.py`'s per-*value* encryption model — it
  needs its own change, not the whole-blob `EncryptedJSON` approach.
- **Masking `headers`/`metadata_` on render** (#12) — pasted tokens in `headers`
  are still echoed by the connector detail/edit views; masking the read-only view
  is safe, the edit form is a usability trade-off. Small follow-up.
