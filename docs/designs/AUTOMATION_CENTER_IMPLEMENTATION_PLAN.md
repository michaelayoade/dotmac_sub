# Automation Center implementation slices

This is the local implementation sequence for the native Automation Center.
It is intentionally separate from deployment and from Frappe/ERPNext. The
application remains the source of truth and scripts are only adapters over
declared module owners.

## Branch sequence

The local feature branches were created in dependency order:

1. `feat/automation-center-foundation` — typed mechanism contracts, script
   target declarations, script control-plane tables, SOT ownership, and the
   module-aware Automation Center UI.
2. `feat/automation-center-capability-registry` — closed trigger/action and
   script-target coverage for the requested modules.
3. `feat/automation-center-project-rules` — project trigger/action adapters
   through the project lifecycle owner.
4. `feat/automation-center-sales-rules` — lead, quote, and sales-order owner
   adapters.
5. `feat/automation-center-customer-support-rules` — customer and support
   owner adapters and conflict checks.
6. `feat/automation-center-operations-rules` — work-order and material-request
   owner adapters.
7. `feat/automation-center-vendor-rules` — vendor-project lifecycle adapters.
8. `feat/automation-center-script-governance` — immutable versions,
   permissions, source policy, audit/run evidence, and publication checks.
9. `feat/automation-center-client-script-runtime` — browser form adapter for
   declared client events, source-integrity verification, and a read-only API
   surface for validation and controlled field changes.
10. `feat/automation-center-server-script-runtime` — external OCI runner
    protocol, digest pinning, isolated execution, and typed command-result
    mapping.
11. `feat/automation-center-unified-builder` — one authoring entry point for
    rules, client scripts, and server scripts.
12. `feat/automation-center-workflow-guidance` — operator guidance and
    module-specific how-to content.
13. `feat/automation-center-validation` — architecture, migration, browser,
    and integration validation.

The slices were committed locally in the sequence above. The assembled review
tip is `feat/automation-center-validation`; the branch tips are intentionally
kept as separate local review checkpoints so each dependency boundary can be
inspected independently. No branch has been pushed, merged, deployed, or used
for server changes in this work session.

The local checkpoint commits are:

| Slice | Commit |
| --- | --- |
| Foundation | `78e9cec61` |
| Capability registry | `eb258ff10` |
| Project rules | `ccee5a08c` |
| Sales rules | `6f1d85156` |
| Customer/support rules | `91ccfb853` |
| Operations rules | `ae87d6207` |
| Vendor rules | `20b0156c4` |
| Script governance | `7a79c3dd3` |
| Client runtime | `763665ae0` |
| Server runtime | `bd66fa75c` |
| Unified builder | `53df3c0ac` |
| Workflow guidance | `190882020` |
| Validation | `7879a7e88` |

The validation tip also contains the follow-up typed sales-input correction and
the vendor-project label clarification. The only intentionally untracked local
artifact is graphify's analysis output; it is preserved and is not part of the
implementation.

## Current readiness matrix

| Module | Script target | Rule draft | Rule publication |
| --- | --- | --- | --- |
| Support tickets | Yes | Yes | Existing reviewed support actions only |
| Customer accounts | Yes | Yes | Status actions use the reviewed account-status owner |
| Leads | Yes | Yes | Status action uses Lead authoring owner |
| Quotes | Yes | Yes | Non-accepted status action; acceptance remains separate |
| Sales orders | Yes | Yes | Draft/confirmed/cancelled only; paid/fulfilled remain evidence-owned |
| Projects | Yes | Yes | Status action uses project lifecycle owner |
| Work orders | Yes | Yes | Status adapter stages through the native work-order owner |
| Material requests | Yes | Yes | Cancellation action uses the ERP outbox consumer |
| Vendor projects | Yes | Yes | Status action uses the vendor lifecycle coordinator |

“Yes” for rule draft means the module, event, and owner action are closed,
permissioned, and runtime-admitted in the registry. Publication still checks
permissions, conflicts, idempotency, audit evidence, and the owner command at
activation time. “No” means the Center intentionally exposes the target for
scripts but does not offer native rule authoring yet.

## Runtime decision

The stack already has a rootless Podman/OCI boundary for external runners. The
server-script slice should reuse that transport shape with a separately pinned
JavaScript runtime image. The application process must not embed Python,
QuickJS, Deno, or unrestricted JavaScript evaluation. The runtime image is
deployment configuration (`AUTOMATION_SCRIPT_RUNTIME_IMAGE` plus a full
`sha256:` digest), default-deny network, read-only filesystem, dropped
capabilities, no-new-privileges, bounded memory/processes, and a typed JSON
wire contract. Publication remains unavailable until that runtime is present.
