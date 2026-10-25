# ADR 0033: OLT SSH host-key observations are out-of-band evidence

Status: proposed

Date: 2026-10-25

Decision owner: Michael (D3 approved 2026-10-25: record first, enforce later)

Affected systems and domains: OLT SSH control plane (`app.services.network.olt_ssh`),
admin OLT detail page, observability.

## Context

OLT SSH logins performed no host-key verification. D3 adds trust on first use:
record the key an OLT presents, warn on change, and optionally refuse
(`olt_ssh_host_key_enforcement = enforce`, off by default).

## Decision

1. **Authority.** `olt_devices.olt_ssh_host_key_*` holds the pinned public
   fingerprint; `olt_ssh_presented_key_*` holds the last key a login presented
   that is not the pinned one. Public-key evidence only; no secret is stored.
2. **Out-of-band writer.** `network.olt_ssh_host_key_evidence`
   (`app/services/network/olt_ssh_hostkey.py`, `record_observation`) is declared
   `out_of_band_evidence`. It runs after the SSH login in its own short
   `create_session()` transaction with a 200 ms lock timeout, so the evidence of
   a refused or contradicted login survives and the SSH path never waits on the
   caller's row locks. Each step is one conditional UPDATE; first sight is
   `WHERE sha IS NULL`, so concurrent logins cannot overwrite a pinned key. It
   never replaces a pinned key.
3. **Trust is an explicit admin command**, `pin_presented_key`, which pins exactly
   the fingerprint the operator reviewed and only while it still equals the
   presented fingerprint. It runs in the request transaction with its audit
   event, is admin-only, and also works when no key is pinned, so `enforce`
   cannot lock an OLT out permanently.
4. **Fail-safe mode.** An unreadable setting uses the last mode read by the
   process, so an outage never downgrades `enforce`; with no prior value the
   default is `record`.

## Consequences

`record_observation` is the only out-of-band write; it is fail-soft and drops
evidence rather than failing a login. Enforcement activation per OLT is a later
decision.

## Verification

`tests/test_olt_ssh_hostkey.py`, `tests/architecture/test_out_of_band_evidence_ratchet.py`,
`tests/architecture/test_undeclared_out_of_band_writers.py`.
