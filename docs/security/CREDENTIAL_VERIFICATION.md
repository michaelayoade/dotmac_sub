# Credential verification boundaries

`auth_flow.verify_user_password(password: str, password_hash: str | None) -> bool`
verifies `UserCredential.password_hash` for login, password change and step-up.
`auth_flow.verify_service_secret(password: str, secret_hash: str | None) -> bool`
verifies the shared `AccessCredential` fallback. There is no ambiguous
`verify_password` alias. Both are verification-only and never write stored values.

The existing PBKDF2-SHA256, sha512_crypt and reversible compatibility remains.
User bcrypt verification keeps the direct bcrypt implementation: `$2a$`, `$2b$`
and `$2y$` are supported, `$2x$` and inputs over 72 UTF-8 bytes fail closed.
The shared service fallback continues to refuse every bcrypt variant. The
customer portal's separate direct decrypt-and-compare PPPoE fallback is unchanged;
it must not be replaced with the broader legacy-hash verifier without a separate
acceptance decision. PPPoE/RADIUS storage and protocol behavior are unchanged.

Reversible user credentials (`plain:`, `enc:` and secret references) remain
accepted temporarily. B-PR3 must deliver the recovery route and audited refusal
setting before the separately approved enforcement cutover. This slice adds no
Argon2 parameters, hash upgrades, password creation changes or new format support.
Unknown/malformed hashes and decryption failures return `False`. The aggregate
`credential_format_rejections_total` metric has no labels, identifiers or hashes.

`tests/architecture/test_credential_verifier_boundary.py` prevents ambiguous
calls/imports/mock targets and pins user and service caller separation. Existing
legacy bcrypt tests and `tests/test_credential_verifiers.py` exercise format and
failure compatibility; PostgreSQL session/storage acceptance remains a separate
integration gate.
