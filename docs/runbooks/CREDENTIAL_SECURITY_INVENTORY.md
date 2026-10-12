# Credential security aggregate inventory

`auth.credential_security_inventory` owns a read-only census of
`user_credentials`. Source work only: no production measurement has been made.
Michael must authorize a future production run and name its target host.

On an authorized development or operator host with its established database
configuration, use:

```sh
python -m scripts.authentication.credential_security_inventory --mode read-only
```

The CLI accepts only read-only mode and emits JSON schema version 1. It uses the
existing PostgreSQL REPEATABLE READ / READ ONLY session, rolls back on exit, and
refuses other database engines or a dirty/write-capable session. The database
returns only fixed CASE labels and counts, never credential identifiers,
usernames, hashes, secret references or secret values. Nothing resolves or
decrypts secrets. Error output is fixed text, without SQL or exception details.
AccessCredential and RadiusUser are outside the query.

Each nonempty cohort has principal (customer/staff/reseller/invalid), provider
(local/external), format, password age, last-login age, active status and count.
Principal invalid means zero or multiple legacy principal links. Provider is a
bounded classification, not a raw provider name. Formats identify stored
prefix families only; malformed values with a known prefix retain that family,
`$2x$` and unrecognized values are unknown, and NULL/empty values are missing.
Secret-reference prefixes match the existing reference contract (`bao://`,
`openbao://`, `vault://`, `env://`) without importing its resolver.

A single UTC observation time drives both age dimensions: future, under 30 days,
30–89 days, 90–364 days, and 365 days or more. Exact cutoffs enter the older band.
NULL timestamps are unknown; password age uses only password_updated_at and
never substitutes account creation or representation-update time. NULL login
time cannot prove a login never happened. Historical PPPoE logins may have
stamped last_login_at, so this field is not proof of a local-password login.

The reversible summary counts plain/encrypted/reference families across every
principal/provider/activity cohort. The dormant legacy summary counts active
local PBKDF2/bcrypt/SHA512 cohorts with unknown or at-least-90-day last login.
These are inventory candidates, not enforcement decisions. D4's reset deadline
starts 90 days after the upgrade-capable release is enabled (T0), which this
report neither assumes nor changes. Unknown values require separate authorized
investigation; they are never dumped as samples.

This report is rebuilt on demand from one snapshot and is not persisted. Compare
the same UTC observation time/snapshot when checking repeatability; later
snapshots can change as authoritative credentials change. No reset, notification,
disablement, rehash or setting change occurs. The B-PR1 portal-login `src` runtime
metric is deliberately split into the portal authentication slice to avoid
overlapping portal worker files; this census does not claim usage measurement.
