# Finite staging provisioning source

This packet is source only. The typed agent, resolver and runner inputs perform
finite local operations on enrolled Seabone/hp-server hosts, without activation.
They do not implement remote execution, release discovery or self-updates.

Independently authenticated public release manifests, source revisions, SHA-256
pins, detached signatures and a separately approved public signer are required.
`VerifiedArtifact.verify` defines the exact signed statement; runner statements
bind actions/runner, revision and digest. No verified cosign, SmartDNS or runner
manifest is supplied here, so package readiness is unproved. Never invent pins
or sign an unverified download merely to satisfy these types. Agent installation
additionally verifies the existing exact main-workflow Sigstore identity.

The existing root-owned Python 3.12.15 environment is preserved and checked for
cryptography 50, cffi 2.1 and pycparser 3.0. Exact gh, Docker, Git, Bash and flock
bytes and authenticated manifest pins are mandatory inputs too; mismatching
installed files refuse rather than upgrading them. The dedicated nologin agent
account receives Docker membership, which is host-root-equivalent. An account
created before failure remains; automatic account deletion is deliberately absent.

Files must be absent or identical with the expected mode. Foreign content,
symlinks and writable parents refuse. Rollback removes captured new files only
while unchanged, then empty created directories. No service, ruleset, DNS or
account deletion occurs. A generated agent-owned 0600 private key remains on-host;
reruns preserve it. Only the public-key SHA-256 is returned. Independently read back
ownership, protected parents, hashes and fingerprint before explicit enrollment
and activation approval. Checkout permissions, executor lock and existing credential
stores remain checked prerequisites. No automatic service start is provided.

SmartDNS installation reuses the existing renderer in log-only mode. It loads no
table and starts no resolver. Reviewed build evidence must identify native nftset
TTL support. Actual A/AAAA insertion, refresh, expiry and kernel TTL rotation remain
dedicated-server and staging gates. Enforcement requires a separately approved
operation and captured owned-table state.

The signed pinned runner archive is checked for links, traversal, special files,
duplicates and bounds before extraction. It installs root-owned package files only.
Human-held registration custody, a dedicated unprivileged account, protected
credential placement, disabled updates and an exact repository/label/service
registration receipt remain required. No token is minted or accepted here. The
expected unit is `actions.runner.michaelayoade-dotmac_sub.Seabone-proof1.service`;
verify its registration receipt first. Main and feature-negative proof1 runs must
finish before stop/readback-inactive and proofs3/4. No registration claim is made.

The loopback transport requires a root-protected known-hosts file/hash, exact SSH
authority/user and independently observed canonical Bao cluster ID. These inputs
are not supplied. The pinned authority must expose that same root-namespace Bao
on its own loopback:8200; the WG gateway is not silently substituted. SSH verifies
the host and forward setup, then checks public cluster health before yielding a
loopback endpoint. Redirects/proxies are refused; lifetime and cleanup are bounded.
Existing client URL checks stay unchanged. SSH credentials remain in existing
protected custody, and no credentials belong in this packet.

There is no live provisioning, tunnel, runner registration, TTL acceptance,
fingerprint approval, activation, proof PASS or production authorization evidence.
