"""Canonical SOT declarations for the migration_source domain.

One owner, and it owns only reads. Sub is `asm-dotmac-sub-legacy` in the
accepted ISP replacement programme, and this domain is how it hands its own
facts to a future destination without either side gaining authority over the
other's.

The domain exists separately from `party_identity` and `customer_context`
because the export spans both and belongs to neither: an export owner living
inside one of them would look like that domain had grown a second
responsibility, and would make the cohort boundary depend on which domain shard
somebody happened to open.
"""

from __future__ import annotations

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
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="migration_source",
    services=(
        SOTService(
            name="migration.cohort_export",
            module="app.services.migration_source_export",
            owns=(
                "cohort-isp-01 typed export snapshot",
                "cohort-isp-01 comparison digest",
                "cohort export tenant scope refusal",
                "cohort export contract version admission",
            ),
            depends_on=(
                "party.registry",
                "customer.accounts",
                "customer.branding",
                "access.subscription_lifecycle",
                "tenancy.operator_tenant",
            ),
            notes=(
                "Read-only. It writes nothing, completes no transaction, and "
                "decides nothing about the destination: there is no target "
                "status vocabulary and no disposition field in the contract. "
                "Sub remains the sole production writer of every cohort-1 "
                "fact until a separately authorised sealed switch."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="cohort-isp-01 typed export snapshot",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Party identity record",
                            "canonical Subscriber account record",
                            "canonical brand profile record",
                            "canonical Subscriber lifecycle projection",
                            "operator tenant identity",
                        ),
                    ),
                    ConcernContract(
                        name="cohort-isp-01 comparison digest",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Party identity record",
                            "canonical Subscriber account record",
                            "canonical brand profile record",
                            "operator tenant identity",
                        ),
                    ),
                    ConcernContract(
                        name="cohort export tenant scope refusal",
                        role=OwnerRole.POLICY,
                        input_names=("operator tenant identity",),
                    ),
                    ConcernContract(
                        name="cohort export contract version admission",
                        role=OwnerRole.POLICY,
                        input_names=("accepted Governance cohort definition",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical Party identity record",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "parties, party_roles, party_relationships, "
                            "party_memberships, party_contact_points and "
                            "party_external_references"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Subscriber account record",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "subscribers, subscriber_contacts, addresses, "
                            "organizations and organization_memberships"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical brand profile record",
                        owner="customer.branding",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="brand_profiles",
                    ),
                    AuthorityInput(
                        name="canonical Subscriber lifecycle projection",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Subscriber status, is_active and lifecycle override "
                            "columns, exported as declared derived fields so the "
                            "destination recomputes rather than adopts them"
                        ),
                    ),
                    AuthorityInput(
                        name="operator tenant identity",
                        owner="tenancy.operator_tenant",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "the single operator tenant; a request naming any "
                            "other tenant is refused, never answered empty"
                        ),
                    ),
                    AuthorityInput(
                        name="accepted Governance cohort definition",
                        owner="external:dotmac_governance",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "programmes/dotmac-isp-replacement.json at the "
                            "accepted revision pinned in "
                            "app/migration_source/programme.py"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The caller owns the session. Every export pins it to "
                        "REPEATABLE READ, READ ONLY and never commits, flushes "
                        "or rolls back."
                    ),
                    locking=(
                        "No locks are taken. A repeatable-read snapshot gives "
                        "one consistent view across twelve statements without "
                        "blocking any production writer."
                    ),
                    idempotency=(
                        "The same checkpoint at the same source revision "
                        "returns the same page and the same digests. Ordering "
                        "is by primary key and pagination is keyset, so a "
                        "concurrent insert cannot reshuffle a drain."
                    ),
                    retries=(
                        "Safe to retry without limit; a read that changes "
                        "nothing has nothing to compensate."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "migration.cohort_export.cross_tenant_refused",
                        "migration.cohort_export.unsupported_contract_version",
                    ),
                    mapping_owner=(
                        "the cohort export CLI and any authenticated export adapter"
                    ),
                    fail_closed_on=(
                        "migration.cohort_export.cross_tenant_refused",
                        "migration.cohort_export.unsupported_contract_version",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.INVENTORIED,
                    new_owner="migration.cohort_export",
                    old_owner=(
                        "no previous owner; Sub had no cohort export path, and "
                        "the alternative in use was reading production tables "
                        "directly with an ad hoc script"
                    ),
                    verification=(
                        "Deterministic canonicalisation and digest tests, "
                        "cross-tenant refusal and read-only transaction "
                        "canaries on PostgreSQL, and a static boundary guard "
                        "proving the export path issues no persistence call"
                    ),
                    cutover_gate=(
                        "Governance ctl-isp-006 and ctl-isp-007. This owner "
                        "produces inputs to those controls; it does not verify "
                        "them, and no cohort authority moves because an export "
                        "exists"
                    ),
                    fallback_retirement=(
                        "Ad hoc direct-table reads for migration purposes stop "
                        "being acceptable once this owner exists; the guard "
                        "that would enforce that is the cohort writer ratchet, "
                        "which already fails a new script touching these tables"
                    ),
                ),
                steward="Dotmac Sub technical owner",
                design_refs=(
                    "docs/adr/0012-isp-cohort-source-readiness.md",
                    "docs/ISP_COHORT1_SOURCE_OWNERSHIP.md",
                ),
                test_refs=(
                    "tests/test_isp_cohort_export_contract.py",
                    "tests/architecture/test_migration_export_boundary.py",
                    "tests/integration/test_isp_cohort_export_postgres.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "scripts/migration/export_isp_cohort_snapshot.py",
        "app.services.migration_source_export",
    ),
    rule="Cohort export reads authoritative records through one owner, under a "
    "read-only repeatable-read snapshot, and emits only Sub's own facts. It "
    "never writes, never decides a destination's state, and never accepts a "
    "tenant scope from its caller.",
)
