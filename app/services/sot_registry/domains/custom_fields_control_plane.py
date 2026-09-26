"""Canonical SOT declarations for the Custom Fields control plane."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT

_DESIGN_REFS = (
    "docs/designs/CUSTOM_FIELDS_CENTER_SOT.md",
    "docs/SOT_RELATIONSHIP_MAP.md",
    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
)

DOMAIN = DomainSOT(
    domain="custom_fields_control_plane",
    services=(
        SOTService(
            name="custom_fields.capability_registry",
            module="app.services.custom_field_capabilities",
            owns=(
                "custom-field module and target declarations",
                "custom-field target identity resolution",
            ),
            depends_on=("customer.accounts",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="custom-field module and target declarations",
                        role=OwnerRole.POLICY,
                        input_names=("SOT domain custom-field declarations",),
                    ),
                    ConcernContract(
                        name="custom-field target identity resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "SOT domain custom-field declarations",
                            "canonical target records",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="SOT domain custom-field declarations",
                        owner="custom_fields.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="immutable declarations attached to canonical DomainSOT records",
                    ),
                    AuthorityInput(
                        name="canonical target records",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="registered module owner identity tables",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="registry resolution and target existence checks never write",
                    locking="target identity checks read committed canonical rows",
                    idempotency="the same checked-in registry and target identity resolve identically",
                    retries="read-only resolution is safe to retry",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "custom_field_capability_undeclared",
                        "custom_field_capability_ambiguous",
                        "custom_field_capability_manifest_invalid",
                    ),
                    mapping_owner="custom-field owner and web/API adapters",
                    fail_closed_on=(
                        "unknown, duplicated, incomplete, or runtime-unmapped targets",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="custom_fields.capability_registry",
                    verification="custom-field capability and runtime registry tests",
                    cutover_gate="only a checked-in target declaration accepts definitions or values",
                    fallback_retirement="remove both the declaration and its runtime adapter",
                ),
                steward="platform administration",
                design_refs=_DESIGN_REFS,
                test_refs=("tests/architecture/test_custom_field_boundary.py",),
            ),
        ),
        SOTService(
            name="custom_fields.records",
            module="app.services.custom_fields",
            owns=("custom-field definitions and typed entity values",),
            depends_on=(
                "custom_fields.capability_registry",
                "events.dispatcher",
                "observability.audit_log",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="custom-field definitions and typed entity values",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed custom-field commands",
                            "registered target contracts",
                            "tenant-scoped custom-field records",
                        ),
                        canonical_writer="custom_fields.records",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed custom-field commands",
                        owner="custom_fields.records",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="typed lifecycle and value commands with actor and permission evidence",
                    ),
                    AuthorityInput(
                        name="registered target contracts",
                        owner="custom_fields.capability_registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="closed target, identity, path, permission, and limit declarations",
                    ),
                    AuthorityInput(
                        name="tenant-scoped custom-field records",
                        owner="custom_fields.records",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="CustomFieldDefinition and CustomFieldValue rows",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="each public command enters execute_owner_command once and commits data, audit, and event atomically",
                    locking="definitions and existing values are locked before lifecycle or value mutation",
                    idempotency="tenant target/key and tenant definition/target uniqueness converge duplicate writes",
                    retries="retry the complete typed command after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "custom_fields.records.not_found",
                        "custom_fields.records.permission_denied",
                        "custom_fields.records.target_undeclared",
                        "custom_fields.records.target_not_found",
                        "custom_fields.records.target_mismatch",
                        "custom_fields.records.definition_invalid",
                        "custom_fields.records.key_conflict",
                        "custom_fields.records.options_invalid",
                        "custom_fields.records.validation_invalid",
                        "custom_fields.records.value_invalid",
                        "custom_fields.records.value_required",
                        "custom_fields.records.status_conflict",
                        "custom_fields.records.active_definition_locked",
                        "custom_fields.records.active_limit_reached",
                        *owner_command_boundary_error_codes("custom_fields.records"),
                    ),
                    mapping_owner="custom-field web and API adapters",
                    fail_closed_on=(
                        "undeclared target or missing central/module permission",
                        "inactive definition, invalid typed value, or sensitive-field access",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "custom_field.definition_changed",
                        "custom_field.value_changed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility="events identify field and target but never carry field values",
                    replay="definition/value rows plus audit events reconstruct current state and transitions",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="custom_fields.records",
                    verification="owner, migration, permission, RLS, registry, and UI boundary tests",
                    cutover_gate="all new central custom fields use typed owner commands",
                    fallback_retirement="no legacy migration or dual-write is enabled",
                ),
                steward="platform administration",
                design_refs=_DESIGN_REFS,
                test_refs=(
                    "tests/test_custom_fields.py",
                    "tests/architecture/test_custom_field_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.services.custom_field_capabilities",
        "app.services.custom_fields",
        "app.services.web_custom_fields",
        "app.web.admin.custom_fields",
    ),
    rule=(
        "Only code-registered entity types may receive centrally owned custom fields; "
        "legacy fields remain separate until an explicit migration is designed."
    ),
)
