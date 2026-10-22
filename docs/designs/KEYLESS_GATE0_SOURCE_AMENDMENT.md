# Keyless Gate 0 source amendment

Michael approved the complete source-only amendment on 2026-10-22. It covers
the existing staging signer, pull agent, ledger, five proofs and finite
provisioning adapters. Publication requires ordinary protected review and exact
CI. This amendment grants no live activation or production authorization.

The approved source accounting counts adopted operational helpers, tests,
fixtures, metadata, documentation and both physical contract copies. Existing
owners contribute their reviewed keyless changes; complete files introduced by
the gate are counted in full. The reconciled conservative baseline is 10,053
nonblank/noncomment lines, including 3,486 test lines.

| Component | Maximum |
| --- | ---: |
| All counted source | 13,000 |
| CP Python / mandatory metadata / combined | 1,450 / 300 / 1,750 |
| Agent runtime / each deployment-contract copy | 1,100 / 450 |
| Launcher, unit and public configuration | 150 |
| CI changes | 350 |
| OpenBao, credential helpers and private transport | 1,400 |
| Egress renderer and configuration | 150 |
| Proof and restore runtime, including adopted diagnostics | 1,400 |
| Finite provisioning, including adopted runtime wrappers | 950 |
| Required tests / approved predecessor guard runtime | 4,400 / 60 |

Each ceiling binds independently. An excess stops for a measured decision;
security checks and negative cases cannot be removed to meet a line count.
The current CP evidence and proof drafts are stopped and are not part of this
finite provisioning slice. No excess is accepted by publishing that slice.

The existing deployment, CP, agent, candidate and restore owners remain the
authorities. One authenticated CP gate-evidence adapter is permitted, using the
kernel authentication owner and existing ledger transaction. It must verify
scope and authoritative signed references, preserve immutable retries, and
refuse conflicting evidence or unsuccessful publication.

The host key follows the implemented agent-owned 0600 contract; configuration,
keyring and unit are root-owned and agent-unwritable. Docker membership is
host-root-equivalent. Workflow signature checks do not contain compromise of
an account with unrestricted Docker access.

All five committed live proofs, immutable ledger evidence, Michael's signoff,
the next-morning rerun of proofs 2–4, and separate authorization of the same
accepted production digest remain required. The old Lane 3 execution remains
stopped until replacement acceptance. Credential provisioning is closed and
must not be repeated as part of these adapters.
