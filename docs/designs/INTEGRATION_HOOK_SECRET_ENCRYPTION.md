# Integration hook secret encryption at rest

**Date:** 2026-06-29
**Context:** security-review finding #6 (sibling of the connector-secret work).
`IntegrationHook.auth_config` (bearer token / basic password / HMAC secret) was
stored as plaintext in a JSON column and read raw at execute time.

## Approach — per-value encryption (matches the rotation framework)

Unlike connectors (whole-blob `EncryptedJSON`), hooks already participate in
`credential_key_rotation` via `_rotate_integration_hooks`, which rotates the
**individual** secret values. So hooks use per-value encryption, keeping that
rotation path working:

- **Single source of truth:** `SECRET_AUTH_CONFIG_KEYS` now lives on the model
  (`app/models/integration_hook.py`) and is imported by both the service and
  `credential_key_rotation` (removing the duplicated constant).
- **write:** `integration_hooks.create_hook` / `update_hook` run
  `_encrypt_auth_config`, which `encrypt_credential`s each secret-keyed value
  (idempotent — already-`enc:` values are left alone; non-secret keys like
  `username` are untouched).
- **use:** `_execute_http_hook` decrypts `token` / `password` / `secret` via
  `_decrypt_auth_secret` before building the outbound auth header — so the wire
  value is the plaintext, never ciphertext. `decrypt_credential` treats unprefixed
  legacy values as plaintext, so old rows keep working.
- **edit form:** values are decrypted for display (the form's long-standing
  behaviour) and re-encrypted on save. No DB schema change (`auth_config` stays
  JSON; only the values are wrapped).
- **rotation:** unchanged — `_rotate_integration_hooks` already iterates the same
  secret keys and now has real `enc:` values to rotate.

## Backfill

`scripts/one_off/encrypt_integration_hook_auth_config.py` re-encrypts legacy
plaintext secret values (dry-run default, `--apply`). Idempotent; a key rotation
also encrypts any remaining plaintext.

## Tests

`tests/test_integration_hook_secret_encryption.py` — secrets wrapped at rest /
non-secret keys untouched / use-path round-trips / execute sends the decrypted
token / legacy plaintext still readable. Existing hook service, web-admin, and
key-rotation suites pass unchanged.
