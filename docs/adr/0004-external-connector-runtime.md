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

## Open decisions

These are recorded as unresolved rather than assumed, and each blocks a later
phase rather than Phase 1.

1. **Container runtime and privilege model** (blocks Phase 3). Mounting a
   Docker socket into any component is root-equivalent on the host and would
   hand a connector authority over the machine that is meant to contain it.
   Rootless Podman, a dedicated runner host, and a trusted supervisor that owns
   the socket are the candidates; they differ materially in operational cost.
2. **First external connector** (blocks Phase 6). Flutterwave v4 delivers real
   value but couples a payment integration to a new execution path. A trivial
   connector first proves the rails at lower risk.
3. **SDK scope** (blocks Phase 2). A Python-only SDK matches the current stack.
   A language-agnostic contract is only worth its cost if Dotmac intends to run
   connectors it did not write.
4. **Built-in connectors currently execute in web processes.** The design
   states built-in connectors "execute in integration worker processes rather
   than FastAPI web processes". They do not: interactive capability calls such
   as payment public-key retrieval and connection validation run inline in
   FastAPI request handlers. Correcting this is not purely mechanical, because
   inbound webhook signature verification is specified as synchronous and
   interactive provider commands are synchronous by nature. The deviation is
   recorded here and needs its own decision; it is deliberately out of scope
   for Phase 1.

## Related

- `docs/designs/INTEGRATION_PLATFORM_SOT.md` — runtime and trust tiers, the
  specification this ADR implements.
- `docs/designs/CONNECTOR_SECRET_ENCRYPTION.md` — at-rest connector credential
  encryption, which remains the storage counterpart to in-memory delivery.
- ADR 0001 — typed source-of-truth architecture manifest.
