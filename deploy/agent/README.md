# Staging pull adapter

This source owns the staging outbound poll/verify/report loop. CP owns
`contracts.py`; copy that file byte for byte from `vendor_cp.deploy_agent`,
including conformance vectors. Sub app/kernel code is never imported.
`HostIO` observes Docker, named `.env` pins, config bytes, Git ancestry and
HTTP/Celery health. It invokes only
`/usr/bin/bash scripts/deploy_staging.sh <digest>`.

CP issuance owns independent upstream candidate evidence validation: the exact
successful `release-candidate.yml` evidence run and its immutable candidate
artifact must bind revision, tree, product-manifest byte SHA-256, build run,
evidence run and image digest before signing. The agent does not accept a
caller assertion as that upstream verification and does not duplicate its
GitHub artifact reader. Host verification binds signature/provenance identity,
repository, digest and resolved Git revision, then independently observes the
running revision after execution. The remaining candidate facts are bound
through the trusted CP issuance contract; no host metadata container runs.

The source is not activation evidence. Provisioning and live Gate 0 proofs
remain separate. Michael's 2026-10-21 bounded source-budget amendment permits
normal source review and CI; the CP `docs/deploy-agent/increment2-acceptance.md`
records its ceilings and unchanged live gates. The reviewed engine must implement the locked
`DEPLOY_EXPECTED_FROM_IMAGE` precondition before activation. Pin its exact
SHA-256 in `guarded_executor_sha256`; an unpinned or changed engine fails
closed. The agent also checks signed `from_image_digest` against its initial
observation. Its deploy-lock probe is advisory; the executor owns the atomic
check under the existing deployment lock. Checkout changes remain operator-owned.

Build with `python3 deploy/agent/build.py agent.pyz`. The main-only manual
`deploy-agent-release.yml` builds and keylessly signs the package. A reviewed
provisioning operation installs one immutable package and Sigstore bundle,
root-owned directories and files, and a public checksum pin. The launcher
checks both the pinned SHA-256 and the exact workflow identity before
execution. It never downloads or updates itself. The root-owned runtime
Python virtualenv must use a separately provisioned Python 3.12 runtime and contain
`cryptography`; the current operator packet pins Python 3.12.15. The operating
system interpreter remains separately managed. `cosign`, `gh`, Docker, Git,
Bash and `flock` are host prerequisites. Pin and verify tool installation
through the provisioning owner. No credentials belong in this package.

Install config and signer keyring root-owned without group/world write;
host private Ed25519 PEM is owned by the effective `dotmac-deploy` UID, mode 0600.
The service runs exclusively as `dotmac-deploy:dotmac-deploy`, with supplementary
Docker group access. Root runtime is refused. systemd creates the agent-owned
state and runtime directories mode 0700; the default agent lock lives inside
its runtime directory. Provision the checkout and executor lock for this user;
there is no root-runtime fallback.
Example keyring shape (public material only):

```json
{"keyring_generation":1,"keys":[{"key_id":"desired-state-1","public_key_b64":"<base64url public key>","revoked":false}]}
```

One-pass inspection: `agent.pyz --config <config> --once` prints exactly
`disposition`, `reason`, `report_id`, `executor_spawned`. The staging proof
may add `--desired-file <root-owned envelope>` only with the root-owned
config switch `allow_test_input = true` (disabled by default). This hook also
works without `--once`: the running daemon rereads and checks the root-owned,
non-symlink envelope on every poll, so the proof owner can replace it between
cases. The file must not be group/world writable and is limited to 1 MiB.
Missing or unsafe input fails the poll without fetching desired-state from CP.
It enters the same verification path. Disable the switch after proof collection.
Staging health requires `app` and `celery-worker`; its Compose deployment has
no `celery-beat`. Every observed container must still be running.

`--answer-challenge <challenge.json>` returns the public sealed possession
response; the host observes its own current digest.

The applied signer must be registered and activated by CP's possession
challenge. Use the host's independently observed running digest for its
signed baseline; never copy a digest supplied in a challenge. The configured
host key is loaded internally and never printed. The sealed public envelope
is the only outbound authentication. `gh attestation verify` additionally
uses only the explicit `gh_config_dir` and `docker_config_dir` stores
from root-owned config. Their directories must be root-owned without
group/world write and their credential files readable by the agent; values
never enter agent config or inherited environment. HOME is the agent state
directory. This requires the host's existing read access to the image/attestations API; it
fails closed without that prerequisite. Sub's executor continues to read its
existing deployment gate credential directly from host `.env`. The agent
never inherits shell/CI tokens into verification or executor processes.

A consumed nonce is fsynced before spawn; a crash produces `failed/interrupted`
after independent restart readback, never an automatic resume. Each sealed
report remains durable and retries byte-identically until CP acknowledges.
Replay is journaled without creating a second terminal report for the same
nonce. Invalid payloads which cannot form a valid desired-state join are
quarantined locally by SHA-256 and never fabricated into signed evidence.
Quarantined data and per-run executor logs stay mode 0600; logs may contain
executor output and must be treated as private host evidence.

The single instance lock and nonce journal protect the agent. The executor's
separate deployment lock arbitrates against human/legacy deployments. A busy
probe leaves the nonce unconsumed. Only a previously sealed healthy terminal
report plus fresh full observed match permits a cheap `already_applied`.
Health/config observation errors fail closed and leave consumed work for
restart recovery; they never manufacture success.
