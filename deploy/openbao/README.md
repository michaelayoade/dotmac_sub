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
variable `GATE0_CANARY_SHA256`, and set `OPENBAO_ADDR` as a repository
variable (a public URL, not a secret).

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
D3 placement (amended 2026-10-21): the OpenBao listener is plaintext
HTTP and currently not reachable from arbitrary internet (probe timed
out), so every OpenBao-contacting proof job runs on the staging
self-hosted runner (`dotmac-sub-staging`), which reaches OpenBao over
the existing app path. `OPENBAO_ADDR` must be the staging host's
existing app-path value. Move jobs to hosted runners only after a TLS
OpenBao endpoint exists — never add IP or CIDR conditions to roles.

## Deliberately NOT in Increment 1

No workflow consumes `sub-staging-ci`/`sub-production-ci` yet; no host
changes; no agent; no secret-path widening beyond the two canary leaves;
ERP AppRole is untouched. The `gate0-proof` role is disposable and is
deleted after Gate 0.
