"""Canonical SOT declarations for the secrets_credentials domain."""

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
    domain="secrets_credentials",
    services=(
        SOTService(
            name="secrets.reference_store",
            module="app.services.secrets",
            owns=(
                "secret reference parsing and resolution",
                "OpenBao read/write boundary",
                "bounded secret cache lifecycle",
            ),
        ),
        SOTService(
            name="secrets.deployment_readiness",
            module="app.services.deployment_secret_requirements",
            implementation_modules=("app.services.deployment_secret_schema",),
            owns=(
                "declared deployment secret requirements",
                "deployment secret material readiness",
                "deployment secret schema capability assessment",
            ),
            depends_on=(
                "tenancy.operator_tenant",
                "runtime.db_sessions",
                "integration.installations",
                "integration.registry",
                "secrets.reference_store",
                "financial.prepaid_funding_reconstruction",
                "sales.marketing_conversion_projection",
            ),
            notes=(
                "Read-only early candidate gate. No secret provisioning, rotation, "
                "credential writer or feature enablement. Operation-only absence "
                "does not establish feature readiness; unavailable observations fail closed."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="declared deployment secret requirements",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "operator tenant identity",
                            "machine credential lifecycle",
                            "enabled integration installation configuration",
                            "pinned connector requirements",
                            "referenced secret material",
                            "prepaid verification policy",
                            "conversion material policy",
                            "candidate schema and bootstrap evidence",
                        ),
                    ),
                    ConcernContract(
                        name="deployment secret material readiness",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "operator tenant identity",
                            "machine credential lifecycle",
                            "enabled integration installation configuration",
                            "pinned connector requirements",
                            "referenced secret material",
                            "prepaid verification policy",
                            "conversion material policy",
                            "candidate schema and bootstrap evidence",
                        ),
                    ),
                    ConcernContract(
                        name="deployment secret schema capability assessment",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "operator tenant identity",
                            "machine credential lifecycle",
                            "enabled integration installation configuration",
                            "pinned connector requirements",
                            "referenced secret material",
                            "prepaid verification policy",
                            "conversion material policy",
                            "candidate schema and bootstrap evidence",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="operator tenant identity",
                        owner="tenancy.operator_tenant",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Canonical operator UUID and transaction-local tenant scope",
                    ),
                    AuthorityInput(
                        name="machine credential lifecycle",
                        owner="external:dotmac_kernel",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Hosted MachineCredential lifecycle and attribution columns; never hashes or raw tokens",
                    ),
                    AuthorityInput(
                        name="enabled integration installation configuration",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Enabled installation pins and same-installation current configuration secret references",
                    ),
                    AuthorityInput(
                        name="pinned connector requirements",
                        owner="integration.registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Exact deployed manifest pin and its declared required_secret_names",
                    ),
                    AuthorityInput(
                        name="referenced secret material",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Existing product boot registries, keyring validator and referenced material; values never leave the probe",
                    ),
                    AuthorityInput(
                        name="prepaid verification policy",
                        owner="financial.prepaid_funding_reconstruction",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Attestation is required at manifest verification, not inferred enabled from historical batches",
                    ),
                    AuthorityInput(
                        name="conversion material policy",
                        owner="sales.marketing_conversion_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Operation-scoped conversion material; preserve configured-setting then held-source precedence",
                    ),
                    AuthorityInput(
                        name="candidate schema and bootstrap evidence",
                        owner="external:dotmac_sub_release",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Exact candidate Alembic graph, target PostgreSQL catalogue, and independently verified first-deployment authorization",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Adapter owns one PostgreSQL repeatable-read, read-only operator snapshot; query never flushes or completes it",
                    locking="No write locks; catalogue and requirement reads use the same snapshot",
                    idempotency="Re-evaluation has no persistence or provider effect beyond bounded secret reads",
                    retries="Retry the whole snapshot after repairing unavailable inputs; never treat an error as empty",
                ),
                errors=ErrorContract(
                    domain_codes=("deployment_requirements.timezone_required",),
                    mapping_owner="scripts.setup.verify_deployment_secret_requirements",
                    fail_closed_on=("deployment_requirements.timezone_required",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="secrets.deployment_readiness",
                ),
                steward="Dotmac Sub technical owner",
                design_refs=(
                    "docs/runbooks/PRODUCTION_DEPLOYMENT.md",
                    "docs/PLATFORM_ADOPTION_LEDGER.md",
                ),
                test_refs=(
                    "tests/test_deployment_secret_requirements.py",
                    "tests/test_deployment_secret_schema.py",
                    "tests/test_deployment_secret_readiness.py",
                    "tests/integration/test_deployment_secret_requirements_pg.py",
                    "tests/integration/test_deployment_secret_schema_pg.py",
                ),
            ),
        ),
        SOTService(
            name="secrets.settings_policy",
            module="app.services.domain_settings",
            owns=(
                "secret setting classification",
                "secret setting reference persistence",
            ),
            depends_on=("secrets.reference_store",),
        ),
        SOTService(
            name="secrets.credential_crypto",
            module="app.services.credential_crypto",
            owns=(
                "database credential encryption",
                "credential field inventory",
                "current and previous decryption key resolution",
            ),
            depends_on=("secrets.reference_store",),
        ),
        SOTService(
            name="secrets.access_credential_format",
            module="app.services.access_credential_secret",
            owns=(
                "access credential representation classification",
                "one-way RADIUS hash preservation policy",
                "explicit cleartext marker normalization",
            ),
        ),
        SOTService(
            name="secrets.credential_integrity",
            module="app.services.credential_key_rotation",
            owns=(
                "credential integrity classification",
                "plaintext credential remediation",
                "credential integrity observability projection",
                "credential re-encryption convergence",
            ),
            depends_on=(
                "secrets.access_credential_format",
                "secrets.credential_crypto",
                "observability.recording",
                "runtime.db_sessions",
            ),
        ),
        SOTService(
            name="secrets.rotation",
            module="app.services.credential_rotation_schedule",
            owns=(
                "scheduled credential key lifecycle",
                "rotation grace period",
            ),
            depends_on=(
                "secrets.reference_store",
                "secrets.credential_integrity",
                "runtime.db_sessions",
            ),
        ),
        SOTService(
            name="secrets.credential_recovery",
            module="app.services.credential_lifecycle_cleanup",
            owns=(
                "lost-key credential recovery planning",
                "lifecycle-safe unrecoverable credential cleanup",
                "reviewed cleanup plan digest enforcement",
            ),
            depends_on=(
                "secrets.credential_integrity",
                "network.identity",
                "network.radius_sessions",
                "access.radius_state",
                "runtime.db_sessions",
                "observability.recording",
            ),
        ),
        SOTService(
            name="secrets.settings_migration",
            module="app.services.settings_secret_cleanup",
            owns=(
                "noncanonical secret-setting discovery",
                "OpenBao secret-setting migration",
                "secret-setting reference replacement",
            ),
            depends_on=(
                "secrets.reference_store",
                "secrets.settings_policy",
                "secrets.credential_crypto",
            ),
        ),
    ),
    entrypoints=(
        "app.tasks.security",
        "app.web.admin.system",
        "scripts.one_off.migrate_secret_settings_to_openbao",
        "app.services.*",
    ),
    rule="Bootstrap secrets use environment or mounted files; application "
    "secrets use references; high-cardinality credentials use the "
    "declared encrypted-field inventory. Callers never choose storage.",
)
