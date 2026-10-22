# Gate0 measured proof draft

This source is an isolated, unpublished measurement draft. Shipping caps have
not changed. It does not authorize installation, activation, host changes,
credential custody, a deployment, or production acceptance.

## Owners and trust

`run_proofs.py` is a finite staging adapter. The existing CP
`DeployAgentService` owns candidate approval, desired issuance, signature and
history validation, receipts and GitLedger writes. The installed Agent owns
polling, nonce consumption, sealed reports and HostIO execution. The existing
`scripts/deploy_staging.sh` and locked `scripts/deploy.sh` own deployment and
rollback. The existing release/source-preparation contract is documented in
`docs/runbooks/STAGING_PROMOTION.md`. Proof capture neither becomes a deployment
engine nor changes agent/shared deployment contracts.

Host observations are attributed observations by an enrolled trusted root
operator/tool. CP verifies ledger signatures, exact joins, campaign scope and
published hashes; CP does not claim it independently executed host checks.
The deployment account's Docker access is root-equivalent. These workflow
checks do not contain a compromised Docker-capable account.

## Protected inputs

A reviewed human operator packet must enroll `/etc/dotmac-gate0/enrollment.json`
using the closed `Enrollment` type in `host_evidence.py`. It binds deployment,
config/unit/launcher/runner digests, the exact proof source revision and every
loaded module digest, absolute tool paths and file digests, enrolled public
keys, and the canonical HTTPS GitLedger remote/config digest. Files and every
parent must be root-owned, regular where appropriate, without symlinks or
non-root write permission. Required audit/nft/dig/systemd/Git tools must already
be installed from reviewed pins; the adapter does not install them.

Install the reviewed proof sources separately from the mutable deployment
checkout. Their imports must resolve to the enrolled protected source tree.
The root-owned ledger clone at `/var/lib/dotmac-gate0/ledger` is a read transport
of the existing CP authority. The adapter fetches only `origin/main`, compares
its published head and reads exact desired/report records with bounded retry;
it never commits or pushes. Global Git config, replacement objects and grafts
are disabled for this transport, and checkout hooks/fsmonitor are disabled.

The protected `RunPlan` includes the exact `GateContext`, finite candidate
requests, a current-run exclusive transition handoff for replay, a
controlled DNS rotation hostname, a globally routed disallowed IP/port, and
a hash-pinned typed proof1 observation plan and proof5 public outcome. CP independently binds all campaign
identities, selected candidate plans, action references and expected artifact
hashes. An admin bearer session enters only through the existing protected
process environment `DOTMAC_CP_AUTHORIZATION`; never put it in arguments,
source, public artifacts or logs. No command here provisions credentials.

Replay uses the normal CP desired envelope from this same run’s proof3/4 genuine
accepted `applied` transition, still inside its strict four-hour signed validity
interval. A protected exclusive handoff binds the run/context and exact proof3/4
artifact hashes. Before fixture issuance, the stopped daemon’s normal desired,
original signed applied outbox, canonical nonce journal and published ledger
records must match that transition. Prior-run, altered or non-PASS outcomes
refuse readiness. Both refusal candidates bind their predecessor image to the
forward target image.
No report or nonce is deleted. Bad-signature and rollback fixtures are issued
through the existing authenticated CP preview/issuance owner while the daemon
is stopped. Corruption flips only the first signature byte, preserving the
schema, time and original authoritative join tuple.

## Finite staged invocation

Use the installed reviewed Python environment and module entry point
`python -m deploy.gate0.run_proofs COMMAND --plan PROTECTED_PLAN` only after the
separate live authorization and readiness gates are satisfied.

1. `observe-proof1` reads explicit campaign-bound GitHub run IDs and immutable
   attempt/job records with authenticated, bounded, redirect-refusing queries.
   It extends the existing `GitHubActionsSource` read owner; it does not dispatch
   workflows or create an auth service. A protected `OIDCPlan` binds actor IDs,
   main and feature SHA/ref/run/attempt, workflow ID, reviewed workflow/script
   hashes at both immutable heads, time window and numeric runner identity.
   The runner ID/name/repository must match the enrolled public `.runner`
   settings and unit; `.credentials` is never read. Source hashes and actual
   job/checkout/proof-step conclusions derive the outcome. The expired-JWT row
   must include its owner's 660-second wait. Main's wrong-ref job is skipped;
   the feature wrong-ref job must succeed independently. The feature run's
   positive/aggregate jobs fail by the existing owner's design, so an overall
   feature success cannot substitute. Raw JWT-bearing logs are never fetched.
   The command exports normalized public run/job facts and their canonical
   hashes plus `ProofOutcome`; no caller-provided PASS booleans or arbitrary
   JSON outcome are accepted. Bind those observed hashes into the protected CP
   campaign before final admission.
2. The operator performs only an explicitly authorized exact-source checkout
   under the existing staging source-preparation contract. The ordinary
   `staging-deploy.yml` workflow includes a full deployment and must not be run
   unchanged as a source-preparation or no-op substitute. The agent never moves
   its checkout. No new automatic source writer is introduced here.
3. `observe-prepared-source` independently reads actual HEAD/tree, tracked
   cleanliness, ancestry, running predecessor image/revision and exact candidate
   coordinates. It binds the protected campaign action reference and creates
   an exclusive public `source-preparation.json` observation. It prints
   readiness, never proof PASS. The reviewed operator packet and availability
   of this protected artifact remain live prerequisites; no pre-existing
   workflow export is claimed. Bind its canonical hash into the protected
   RunPlan and CP campaign before `run34`.
4. `run34` refuses missing/mismatched source observation or active runners. It
   captures DNS/kernel state, performs one fresh CP issuance, and observes one
   genuine `applied` report and exactly one executor process. It checks the same
   immutable desired/report/receipt tuple, independent Docker/pins/health,
   cosign/OCI provenance and authoritative published ledger records. An
   `already_applied` report, reused consumed nonce, same-image deployment or
   second executor cannot pass.
5. `run2` performs all three daemon refusal cases. It uses a temporary protected
   systemd override and public temporary config with `allow_test_input=true`.
   The ordinary signed launcher remains the entry point. A fresh kernel audit
   key counts daemon-account executor-script readers independently of reports.
   Journal observations must belong to the fresh systemd invocation. Replay
   preserves the original sealed report bytes. The rollback fixture detaches
   to the signed ancestor while the newer image stays healthy and restores the
   exact prior branch/head before restarting normal polling.
6. `publish --command-id UUID` assembles performed outcomes with the locally derived proof1 and hash-pinned
   proof5 public results (proof5 runs on its enrolled dedicated test host).
   It sends only the existing authenticated `POST /platform/deploy/gate-evidence`.
   An identical second POST must return the same receipt and exact authoritative
   canonical evidence. Matching published PASS is the sole terminal PASS.
   Matching published FAIL is retained and exits nonzero; an absent receipt or
   a push/readback conflict never passes. Failed stages retain partial genuine
   facts; they do not invent reports for missing work.

Artifacts are exclusive root-protected files beneath
`/var/lib/dotmac-gate0/runs/RUN_UUID`. Replacing a run's evidence is refused.
Use a new authorized run for retries that perform new actions. Initial campaign
requires proofs1–5; next-morning context independently requires proofs2–4 with
its protected prior-run relationship. A genuine rerun needs a distinct eligible
forward candidate/source; no no-op commit, same-SHA rebuild or second apply is
created to manufacture it.

## Cleanup and network acceptance

Refusal cleanup removes only byte-matching owned files, restores the exact
checkout, reloads the original unit and observes `nonce_replay` from its fresh
normal invocation after verifying the authoritative normal desired nonce is
already consumed. Active status or a separate HTTP fetch alone is insufficient.
Foreign changes are retained and block restart. An uncertain issuance, malformed returned command or consumed/mismatched nonce
leaves the daemon deliberately stopped; only a fully validated fresh command
allows its explicit start. The operator packet must define recovery. A failed
run can still publish its authoritative FAIL receipt through the read-only
enrollment adapter while the daemon remains stopped.

The controlled hostname must rotate both A and AAAA records. DNS observations
include response TTL and actual kernel timeout/remaining expiry, normalized to
milliseconds using the explicit protected `nft_element_time_unit` enrollment.
A hash-bound `NftUnitFixture` for the exact pinned binary and reviewed upstream
source must establish per-element units on the dedicated test host before
enrollment. Global set timeout documentation alone does not establish ELEM
units; no dump-magnitude guessing is permitted. Until that fixture and source
are verified, the nft binary is not accepted. Both timeout and expiry must fit response TTL plus two
seconds. A read-only concurrent kernel sampler must observe every old address
absent within that TTL plus bounded sampling tolerance while deployment runs.
A long apply cannot conceal late expiry. The current stock SmartDNS candidate
is unsuitable until its three-times-TTL behavior is corrected and verified;
no proof loosens the TTL gate. See the upstream
[SmartDNS implementation](https://github.com/pymumu/smartdns/blob/Release48.4/src/dns_server/context.c#L667)
and [libnftables JSON schema](https://manpages.debian.org/bookworm/libnftables1/libnftables-json.5.en.html).

The disallowed test uses a fresh TCP socket with a bound ephemeral source port.
A unique counter-only rule, matching that exact destination/port/source/new
flow, is inserted directly before the unchanged terminal kernel DROP. Its
counter must increase, the terminal counter must account for those packets,
and the connection must time out. No accept/drop policy is inserted. Cleanup
verifies and deletes only the exact owned rule handle and observes absence.
The insertion attempt itself is inside this cleanup authority: a lost insert
reply triggers bounded tag readback and exact-flow reconciliation. A foreign
flow with that tag is preserved and causes cleanup failure. Lost deletion
replies require repeated actual absence before cleanup can be accepted.
Aggregate unrelated counters or drop-like journal text cannot substitute.
The successful allowed deployment and all network observations bind the same
transition report and observation interval.

## Remaining acceptance

The complete source requires independent review, exact component accounting,
a separate decision for any shipping-cap excess and prescribed hosted CI.
All human custody, public fingerprint verification, actual host enrollment,
source-preparation packet, resolver/binary format acceptance, live proofs,
Michael signoff, next-morning rerun and same-digest production authorization
remain open. Offline crypto/mocked-process tests are not Docker/kernel/live
acceptance. Do not use a local Docker daemon for validation.

After run2, the healthy forward image and exact checkout remain in place and
normal polling has been observed without another executor. The latest normal
CP desired is the consumed rollback fixture and its latest report is
`refused/rollback_digest`; the existing CP status owner therefore reports
`drift=true`. The live operator packet must show that actual status readback.
Gate0 proof PASS does not establish converged CP status or production acceptance;
no restorative no-op apply or ledger rewrite is performed.
