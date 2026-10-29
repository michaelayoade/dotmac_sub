"""Dedicated workbench coordinates Tax policy publication and same-root audit."""

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

_OWNER = "financial.tax_policy_authoring"
_INPUTS = (
    "published Tax policy",
    "staff permission admission",
    "canonical staff identity",
)
_PREVIEW_INPUTS = (*_INPUTS, "sealed prior Tax result evidence")
SERVICES = (
    SOTService(
        name=_OWNER,
        module="app.services.tax_policy_authoring",
        implementation_modules=(
            "app.services.tax_policy_authoring_contracts",
            "app.services.tax_policy_authoring_workbench",
        ),
        owns=(
            "atomic staff Tax policy publication and audit",
            "bounded Tax policy workbench projection",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.staff_provisioning",
            "observability.audit_log",
            "ui.action_form_contracts",
        ),
        notes="Public Tax a7 is the external policy owner. No Sub arithmetic, policy tables, draft preview, historical edits or deletion; no activation.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="atomic staff Tax policy publication and audit",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=_INPUTS,
                ),
                ConcernContract(
                    name="bounded Tax policy workbench projection",
                    role=OwnerRole.RESOLVER,
                    input_names=_PREVIEW_INPUTS,
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name=_PREVIEW_INPUTS[-1],
                    owner="external:dotmac_tax",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source="Public get_tax_determination_set_v2 exact tenant/set UUID read verifies immutable sealed prior evidence; preview derives actual fingerprints and original commercial identity without entered hashes or fabricated amounts",
                ),
                AuthorityInput(
                    name=_INPUTS[0],
                    owner="external:dotmac_tax",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source="Public a7 catalog/history/read-only preview and flush-only publication/supersession contracts",
                ),
                AuthorityInput(
                    name=_INPUTS[1],
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="Server-produced PermissionAdmission from canonical route gate; staff only",
                ),
                AuthorityInput(
                    name=_INPUTS[2],
                    owner="auth.staff_provisioning",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Locked SystemUser activity/Party identity and canonical StaffDisplayIdentityQuery label",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary="One execute_owner_command on a transaction-free session; Tax flush-only publication and staged canonical audit commit or roll back together.",
                locking="Staff principal lock first, then Tax's tenant/code publication lock; Tax unique natural identities arbitrate initial publication races.",
                idempotency="Tax catalog/version exact-content ensure replay; supersession exact source_ref/source_version replay survives later replacement. Every accepted attempt stages its own audit; catalog source refs are provenance, not a global deduplication ledger.",
                retries="Integrity/serialization failures require fresh whole-command retry; conflicts/stale predecessor require fresh history and explicit re-review. Changed material inputs need new source revision.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(
                    f"{_OWNER}.{suffix}"
                    for suffix in (
                        "unauthorized",
                        "invalid_input",
                        "not_found",
                        "conflict",
                    )
                )
                + owner_command_boundary_error_codes(_OWNER),
                mapping_owner="app.web.admin.billing_tax_policy",
                fail_closed_on=(
                    "nonstaff principal",
                    "missing permission/confirmation",
                    "stale predecessor",
                    "conflicting published version",
                    "missing policy",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner=_OWNER,
                verification="Focused typed input, action form, route guard and migrated PostgreSQL canaries; hosted acceptance pending.",
                cutover_gate="Release and verify Tax a6 then a7 before exact a7 pin/lock; full hosted CI, router/RBAC/registry integration and browser verification required. No live policy/activation inferred.",
            ),
            steward="Billing and Tax maintainers",
            design_refs=("docs/designs/TAX_POLICY_AUTHORING_WORKBENCH.md",),
            test_refs=(
                "tests/test_tax_policy_authoring.py",
                "tests/test_tax_policy_dependencies.py",
                "tests/test_tax_policy_authoring_routes.py",
                "tests/integration/test_tax_policy_authoring_pg.py",
                "tests/architecture/test_tax_policy_authoring_boundary.py",
            ),
        ),
    ),
)
