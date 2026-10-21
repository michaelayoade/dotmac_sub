# OpenBao JWT for CI — Increment 1 (Gate 0 design §3/§7.1)

Source-only definitions. Nothing here is applied by CI, and Increment 1
touches no deploy path: `deploy.sh`, `deploy_production.sh`, and
`staging-deploy.yml` behaviour are unchanged. Sub-production-ci is created
but unused until a later increment.

## Apply order (human-run only, on a host that can reach OpenBao)

```sh
export OPENBAO_ADDR=https://<openbao-host>   # loopback HTTP also accepted
deploy/openbao/apply.sh --dry-run            # review the exact writes
export OPENBAO_TOKEN=<admin token>           # never committed, never in CI
deploy/openbao/apply.sh                      # idempotent converge
```

`apply.sh` verifies KV-v2 at `secret/` before writing any policy (D4),
refuses non-loopback plain HTTP (ERP `_validated_openbao_address` rules),
then writes JWT config (`default_role` empty), the three policies, the
three roles in `roles.json`, and seeds both canaries if absent. Canary
values are generated locally and never printed — only `sha256:` digests
appear in output. Copy the staging canary digest into the repository
variable `GATE0_CANARY_SHA256` (a digest of a random canary, not a
secret), and store `OPENBAO_ADDR` as a repository **secret** (never a
variable: variable values appear unmasked in public Actions logs).

`apply.sh` mutates OpenBao (JWT config, ACL policies, roles, canaries,
written with an admin token). It is never run by CI and needs its own
governed apply packet plus an explicit Lane 3 acknowledgement before it is
run.

## Canaries

- `secret/data/dotmac/sub/staging/gate0-canary` — readable by
  `sub-staging-ci` and `gate0-proof`; proof 1 reads it and compares hashes.
- `secret/data/dotmac/sub/production/gate0-negative` — exists so a staging
  token's read returns a policy-meaningful 403 (not a 404 ambiguity).

## Running proof 1

Dispatch **Gate 0 proof 1 (OIDC to OpenBao)** (`openbao-jwt-proof.yml`)
from `main`. The verdict job prints `GATE0 PROOF 1: PASS` iff rows 1–9 of
design §3.4 hold (row 4, wrong ref, is exercised only by also dispatching
from a feature branch; row 10, JWT replay, is accepted residual risk per
D5 and is never asserted). Row 6 sleeps ~11 minutes to expire a real JWT.
D3 placement (amended 2026-10-21): every OpenBao-contacting proof job runs
on the staging self-hosted runner (`dotmac-sub-staging`), from which
OpenBao is reached over the private app path. `OPENBAO_ADDR` (repository
secret) must be the staging host's existing app-path value. Never add IP
or CIDR conditions to roles.

## Deliberately NOT in Increment 1

No workflow consumes `sub-staging-ci`/`sub-production-ci` yet; no host
changes; no agent; no secret-path widening beyond the two canary leaves;
ERP AppRole is untouched. The `gate0-proof` role is disposable and is
deleted after Gate 0.
