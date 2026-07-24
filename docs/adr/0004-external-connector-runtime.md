# ADR 0004: Complete the external connector runtime tier

Status: accepted

Date: 2026-07-23

Decision owner: Michael / Dotmac architecture

Affected systems and domains: integration platform, connector runtime,
connector registry, secret material delivery, egress policy, payment gateway
connectors, and integration operator surfaces

## Context

`docs/designs/INTEGRATION_PLATFORM_SOT.md` already specifies four connector
runtime tiers and describes independently released connectors as "approved,
signed, digest-pinned OCI workloads" that receive no Sub database or Redis
credentials, no host filesystem mounts, no OpenBao master token, and no
unrestricted network.

That specification is not reachable in the deployed system:

- `ConnectorRuntimeType` declares `builtin_worker`, `external_oci`,
  `legacy_adapter`, and `catalogue_only`, and `RuntimeManifest` validates that
  an `external_oci` runtime pins a sha256 image digest.
- Outside `manifest.py` and `registry.py`, the runtime tier is consulted in
  exactly two places, both of which only ask whether a connector is
  installable: `web_integrations.build_marketplace_data` and
  `installations.create_draft`.
- Execution resolves a runner by bare connector key from a hardcoded
  process-local dict in `runtime_execution.default_runner_registry`. No code
  path dispatches on the declared tier, so an `external_oci` manifest would
  silently resolve to whatever happened to be registered under its key, or
  raise a bare `LookupError`.

The result is a platform whose governance layer is complete — manifests,
versions, digests, installations, configuration revisions, capability grants,
validation gates, declared egress, and data-access classification — while its
isolation layer exists only on paper.

Two triggers made this decision current. Flutterwave withdrew v3 API key
issuance for our account, so the Flutterwave connector must be rewritten for a
v4 OAuth model regardless. Separately, Dotmac intends to extend Sub through
connectors rather than through in-repo feature code, which requires a tier
where connector code can ship and run without a Sub deployment.

## Decision

Complete the `external_oci` tier as specified, rather than adding a second
extension mechanism beside it.

1. **Runner resolution becomes manifest-driven.** `ConnectorRunner` selection
   dispatches on `manifest.runtime.type`, not on the connector key.
   `builtin_worker` and `legacy_adapter` resolve from the explicit in-process
   registry as they do today. `external_oci` resolves through a pluggable
   external-runner factory. `catalogue_only` is refused at execution as well as
   at install.
2. **The existing runtime contract is the plugin contract.** External runners
   implement the same `ConnectorRunner` protocol — `validate`, `execute`,
   `health`, `cancel` — and receive the same `OperationEnvelope`, which already
   binds operation ID, correlation ID, installation, capability binding,
   capability ID, connector key and version, sha256 manifest digest,
   configuration revision, trigger, idempotency key, deadline, payload, and
   actor. No parallel envelope, no second protocol.
3. **Failure is closed.** An unresolvable or not-yet-available runtime tier
   raises a typed runtime error before dispatch. It never falls back to another
   tier, another connector's runner, or an unpinned artifact.
4. **Security invariants are inherited from the design, not renegotiated.**
   External runners get no Sub database or Redis credentials, no host mounts,
   no OpenBao master token, and no unrestricted network. They run non-root with
   a read-only filesystem and bounded CPU, memory, and wall-clock. Secret
   material is delivered in memory for exactly one installation binding and
   never enters argv, the database operation payload, Celery arguments, logs,
   traces, audit rows, or runner artifacts.

## Phasing

Each phase lands independently and leaves the system releasable.

- **Phase 1 — manifest-driven resolution.** Dispatch on the declared tier and
  fail closed on tiers with no executor. No behaviour change for existing
  connectors. This ADR ships with it.
- **Phase 2 — runner wire contract and SDK.** Splits in two. The *contract* —
  `app/services/integrations/runner_protocol.py`, a versioned request and
  response schema for the four verbs — is language-agnostic and lands without
  waiting on open decision 3. The *SDK package* a connector author depends on
  does not, because its language is that decision. Transport is deliberately
  excluded: the schema fixes what crosses the boundary, Phase 3 fixes how.
- **Phase 3 — `ExternalOciRunner` and supervision.** Image pull, digest
  verification, signature verification, container lifecycle, resource bounds,
  deadline enforcement, and in-memory secret delivery.
- **Phase 4 — egress enforcement.** `EgressManifest` hosts are declared today
  and enforced nowhere, including for built-in connectors. Enforce at the
  boundary.
- **Phase 5 — operator surface.** Extend the installation admin screens with
  runtime tier, image digest, signature status, resource limits, and
  install/upgrade by digest.
- **Phase 6 — first external connector**, proving the path end to end.

## Consequences

Connector authorship stops requiring a Sub deployment once Phase 3 lands, which
is the point of the exercise. In exchange the platform takes on container
supervision, image trust, and boundary enforcement as first-class operational
responsibilities, and the security invariants above become things that must be
tested rather than asserted.

Until Phase 3, an `external_oci` manifest is registrable and installable but not
executable, and says so with a typed error. This is deliberate: a half-connected
tier that silently degrades to in-process execution would defeat the isolation
the tier exists to provide.

## Resolved decisions

1. **Container runtime and privilege model** (was blocking Phase 3). *Resolved
   2026-07-24: rootless Podman supervised by systemd, on the existing prod
   host.* Each connector operation runs as a short-lived rootless Podman
   container launched and torn down by a systemd unit acting as the supervisor.

   Rationale. The failure this tier exists to prevent is a connector reaching a
   privileged container socket and gaining host root. Rootless Podman removes
   that path structurally rather than by configuration: there is no long-lived
   root daemon or socket to steal, and container-root maps through a user
   namespace to an unprivileged host UID, so even a container escape lands
   unprivileged. A socket-owning supervisor over a root Docker daemon was
   rejected because it keeps the root daemon and makes our own flag-setting the
   single point of failure. It fits the current scale: systemd is already
   present, and "launch, run one operation, tear down" is Podman's model, with
   no new orchestration layer.

   Scope and escalation. This is sized for **first-party** connectors —
   Dotmac's own code decoupled from Sub's deploy cycle. It shares a kernel with
   the payments workload, which is acceptable when the threat is a bug rather
   than an adversary. Running genuinely untrusted third-party connector code
   requires escalation recorded as a future decision: a dedicated runner host
   for blast-radius isolation, and microVM isolation (Kata or Firecracker) so
   each connector gets its own kernel. Escalation is deliberate, not a default;
   the same build path supports it by moving the supervising systemd slice to a
   dedicated host.

   Consequences for later phases. Phase 3 targets the rootless Podman transport
   directly. Phase 4 egress enforcement must use rootless networking
   (pasta/slirp4netns) or an egress proxy rather than a bridged Docker network.
   Secret material is delivered to a container via a tmpfs-backed file
   descriptor or `podman secret`, never argv and never baked into the image;
   rootless execution makes any leak far less dangerous because the holder is
   unprivileged.

## Open decisions

These are recorded as unresolved rather than assumed, and each blocks a later
phase rather than Phase 1.

1. **First external connector** (blocks Phase 6). Flutterwave v4 delivers real
   value but couples a payment integration to a new execution path. A trivial
   connector first proves the rails at lower risk.
2. **SDK scope** (blocks the SDK half of Phase 2). A Python-only SDK matches the
   current stack. A language-agnostic contract is only worth its cost if Dotmac
   intends to run connectors it did not write — the same trust question that
   sets the escalation ceiling in resolved decision 1.
3. **Built-in connectors currently execute in web processes.** The design
   states built-in connectors "execute in integration worker processes rather
   than FastAPI web processes". They do not: interactive capability calls such
   as payment public-key retrieval and connection validation run inline in
   FastAPI request handlers. Correcting this is not purely mechanical, because
   inbound webhook signature verification is specified as synchronous and
   interactive provider commands are synchronous by nature. The deviation is
   recorded here and needs its own decision; it is deliberately out of scope
   for Phase 1.

## Source-of-truth audit per phase

Every phase of this work carries an SOT audit before it is called done. The
external tier is exactly where a parallel decision authority could creep in
unnoticed: a connector and its runner are *transports*, never business-decision
owners, but out-of-process execution and a new wire contract make it easy to
let the runner decide, cache, or project state that a named owner should own.
Each audit confirms the phase stays a thin adapter around existing owners.

Named owners this work must not duplicate or bypass:

- **Installations service** owns config revisions, capability bindings, and
  installation lifecycle.
- **`payment_routing`** owns provider routing and eligibility.
- **Domain command owners** own the consequences of provider observations.
- **The connector manifest** (code/artifact-owned) owns the runtime tier,
  capability, egress, and secret declarations.

The runtime layer executes and returns a sanitized observation; it never
decides a consequence, never persists domain state, and never becomes a second
copy of truth.

**Phase 1 — tier dispatch.** `resolve_runner` is a pure selection over
`manifest.runtime.type`. It decides no business state — not installation state,
config, bindings, routing, or consequence — and reads only the code-owned
manifest to pick a transport. Failing closed on an unexecutable tier preserves
the isolation invariant rather than introducing a decision. No new authority;
the manifest remains the source of the runtime tier. Clean.

**Phase 2 — wire contract.** `runner_protocol.py` is a data schema with no
callers, no persistence, and no behaviour. It carries the `OperationEnvelope`
(produced by `runtime_execution`) and the result types (owned by `runtime`); it
owns neither. Its validators enforce transport shape and the no-secret-on-wire
and connector-pin invariants — supporting the security boundary, not creating a
decision surface. Clean. When Phase 3 gives it callers, its audit re-checks that
the runner gains no authority.

**Phase 3 — external runner, marshalling half.** `ExternalOciRunner` implements
the in-process `ConnectorRunner` protocol by translating each verb to a
`RunnerRequest`, handing it to an injected `RunnerTransport`, and translating
the `RunnerResponse` back. It is a transport adapter and owns no decision:

- It never persists domain state and never decides a consequence. It returns a
  sanitized `OperationResult`; the domain owner that receives it still decides
  what that observation means. The split between observation and consequence is
  preserved.
- Its one piece of judgement is deliberately conservative and hands authority
  *back* to an owner rather than taking it: an ambiguous execute outcome — a
  timeout, or a protocol-violating response from a container that may already
  have acted — becomes `reconciliation_required`, never a silent retry or a
  fabricated success. The reconciler owns the resolution.
- A semi-trusted connector cannot escalate into Sub: every malformed or hostile
  response maps to a fail-closed typed result, so a broken connector cannot
  crash a worker or be mistaken for a healthy one.

No ownership boundary moved, so no `SOT_RELATIONSHIP_MAP.md` or
`sot_relationships.py` change is due for this half. Clean.

**Phase 3 — transport half.** `PodmanTransport` runs the connector's image as a
short-lived, hardened, rootless Podman container: request JSON on stdin,
response on stdout, container removed after. It owns no decision — it moves
bytes and enforces confinement — and its `_build_argv` is a pure function so the
security flags are unit-tested in isolation, with a live integration test on a
real container (validated on seabone) covering the mechanics the unit test
cannot: stdin/stdout, exit codes, the deadline kill, and out-of-band secret
delivery. Audited properties:

- Secrets are written to a tmpfs, user-private, `0600` env file and passed with
  `--env-file`; a secret value never appears on argv (where `ps` would expose
  it) and the file is deleted in a `finally`. The live test asserts the value
  never crosses back across the boundary.
- Confinement is `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-
  privileges`, no host mounts, an in-memory `noexec,nosuid` scratch tmpfs, and
  bounded memory and pids. Rootless maps container-root to an unprivileged host
  uid.
- The app-side subprocess deadline is authoritative and unambiguous; Podman's
  own `--timeout` is a longer backstop that reaps an orphaned container. An
  overrun is therefore always a `RunnerTimeout`, which `ExternalOciRunner` maps
  to `reconciliation_required` — never a silent retry.

No ownership boundary moved. Clean.

**Deployment prerequisites (Phase 3, discovered on seabone).** Rootless Podman;
`subuid`/`subgid` mapped for the runner user; a user systemd instance. memory
and pids cgroup controllers are delegated to a rootless user by default and
enforce out of the box. The **cpu** controller is *not* delegated by default on
Ubuntu 22.04, so CPU limiting is opt-in in the transport rather than defaulted —
setting `--cpus` where the controller is absent fails every operation. A
production host that wants CPU bounds must delegate the controller
(`Delegate=cpu cpuset io memory pids` under `user@.service.d`, then re-login) and
opt in; otherwise memory, pids, and the wall-clock deadline are the enforced
bounds. Egress restriction is Phase 4.

**Phase 4 onward.** Each audit must show the runner does not become a parallel
authority, must update `docs/SOT_RELATIONSHIP_MAP.md` and
`app/services/sot_relationships.py` where a phase changes an ownership boundary,
and must add or adjust an architecture guard test that prevents a parallel path
from returning. Deviations are recorded as explicit decisions, not absorbed
silently.

## Related

- `docs/designs/INTEGRATION_PLATFORM_SOT.md` — runtime and trust tiers, the
  specification this ADR implements.
- `docs/designs/CONNECTOR_SECRET_ENCRYPTION.md` — at-rest connector credential
  encryption, which remains the storage counterpart to in-memory delivery.
- ADR 0001 — typed source-of-truth architecture manifest.
