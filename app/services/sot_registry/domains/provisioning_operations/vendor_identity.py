"""provisioning_operations SOT declarations: vendor identity."""

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
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="operations.vendor_creation",
        module="app.services.vendor_creation",
        implementation_modules=("app.services.dotmac_erp.vendor_supplier_sync",),
        owns=(
            "native vendor creation and ERP supplier delivery intent",
            "ERP supplier identity response projection and repair",
        ),
        depends_on=("integration.backoffice_adapter",),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="native vendor creation and ERP supplier delivery intent",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("staff vendor creation command",),
                    canonical_writer="operations.vendor_creation",
                ),
                ConcernContract(
                    name="ERP supplier identity response projection and repair",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical native vendor record",
                        "ERP supplier identity response",
                    ),
                    canonical_writer="operations.vendor_creation",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="staff vendor creation command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="authenticated staff vendor form and command context",
                ),
                AuthorityInput(
                    name="canonical native vendor record",
                    owner="operations.vendor_creation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the native Vendor identity and its current supplier binding",
                ),
                AuthorityInput(
                    name="ERP supplier identity response",
                    owner="external:dotmac_erp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "the validated source vendor UUID, supplier UUID, supplier code, "
                        "and create-or-replay result returned by Dotmac ERP"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Native Vendor, FieldVendor bridge and ERP supplier outbox intent "
                    "commit atomically; a validated delivery result updates only that "
                    "vendor in the delivery worker's separate transaction."
                ),
                locking=(
                    "New vendor ID is unique; code uniqueness is checked on native and "
                    "portal tables, and a conflicting supplier binding fails closed."
                ),
                idempotency=(
                    "One unique supplier outbox key per native vendor UUID; an accepted "
                    "response may be replayed only for the same source and supplier UUIDs."
                ),
                retries=(
                    "ERP delivery retries after the vendor transaction commits; accepted "
                    "but unlinked results are repaired from durable outbox evidence."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_creation.active_caller_transaction",
                    "operations.vendor_creation.command_contract_violation",
                    "operations.vendor_creation.invalid_command_context",
                    "operations.vendor_creation.nested_owner_command",
                    "operations.vendor_creation.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendors",
            ),
            events=EventContract(
                event_types=("vendor.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="The supplier payload is versioned and keyed by source Vendor UUID.",
                replay="The ERP supplier endpoint returns the existing supplier for a repeated source UUID.",
            ),
            projections=(
                ProjectionContract(
                    name="ERP supplier identity response projection and repair",
                    input_names=(
                        "canonical native vendor record",
                        "ERP supplier identity response",
                    ),
                    writer="operations.vendor_creation",
                    freshness="Updated when a supplier delivery is accepted by ERP.",
                    stale_behavior=(
                        "An absent supplier reference remains explicitly pending; it never "
                        "implies that ERP accepted or created a supplier."
                    ),
                    drift_signal=(
                        "An accepted vendor-supplier outbox event whose native Vendor lacks "
                        "the same Dotmac ERP supplier UUID."
                    ),
                    rebuild_operation=(
                        "Replay repair_accepted over accepted vendor-supplier outbox events."
                    ),
                    repair_owner="operations.vendor_creation",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="operations.vendor_creation",
                verification="Vendor creation, atomic outbox, ERP delivery and repair tests.",
            ),
            steward="vendor operations",
            design_refs=("docs/BACKOFFICE_INTEGRATION_BOUNDARY.md",),
            test_refs=("tests/test_vendor_supplier_sync.py",),
        ),
    ),
    SOTService(
        name="auth.vendor_user_provisioning",
        module="app.services.vendor_user_provisioning",
        owns=(
            "vendor portal login provisioning and revocation",
            "vendor organisation role assignment",
            "vendor portal profile repair and CRM contact import",
        ),
        depends_on=("auth.permission_gate",),
        notes=(
            "One vendor login is a SystemUser marked UserType.vendor, a "
            "must-change local credential, and the FieldVendorUser "
            "membership auth resolves through; any subset is a broken "
            "identity, so all three are staged together. This owner never "
            "mints or delivers a usable secret. Capability for the "
            "assigned role is declared by field.vendor_capabilities; this "
            "owner stores the role and never decides what it may do. "
            "FieldVendor.native_vendor_id is the authoritative unique link; "
            "the string CRM-shaped value is written only for compatibility."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor portal login provisioning and revocation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("vendor portal login command",),
                    canonical_writer="auth.vendor_user_provisioning",
                ),
                ConcernContract(
                    name="vendor organisation role assignment",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("vendor portal login command",),
                    canonical_writer="auth.vendor_user_provisioning",
                ),
                ConcernContract(
                    name="vendor portal profile repair and CRM contact import",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("vendor portal login command",),
                    canonical_writer="auth.vendor_user_provisioning",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="vendor portal login command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "staff-authorized vendor, identity, and role "
                        "identifiers from the admin vendor surface"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Principal, credential, and membership commit together "
                    "or not at all."
                ),
                locking=(
                    "The native vendor row is locked; email uniqueness and "
                    "unlinked portal-profile conflicts are rechecked before writes."
                ),
                idempotency=(
                    "Native vendor-to-portal profile linkage is unique, and "
                    "system-user email uniqueness makes a repeated provisioning "
                    "attempt a refusal, never a duplicate principal."
                ),
                retries=(
                    "No automatic retry: a collision is an identity "
                    "question for staff to resolve."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "auth.vendor_user_provisioning.vendor_not_found",
                    "auth.vendor_user_provisioning.vendor_inactive",
                    "auth.vendor_user_provisioning.email_in_use",
                    "auth.vendor_user_provisioning.email_required",
                    "auth.vendor_user_provisioning.crm_identity_required",
                    "auth.vendor_user_provisioning.name_required",
                    "auth.vendor_user_provisioning.unknown_role",
                    "auth.vendor_user_provisioning.portal_profile_conflict",
                    "auth.vendor_user_provisioning.membership_not_found",
                    "auth.vendor_user_provisioning.membership_inactive",
                    "auth.vendor_user_provisioning.principal_not_vendor",
                    "auth.vendor_user_provisioning.credential_not_found",
                    "auth.vendor_user_provisioning.credential_projection_conflict",
                    "auth.vendor_user_provisioning.active_caller_transaction",
                    "auth.vendor_user_provisioning.command_contract_violation",
                    "auth.vendor_user_provisioning.invalid_command_context",
                    "auth.vendor_user_provisioning.nested_owner_command",
                    "auth.vendor_user_provisioning.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendors",
            ),
            events=EventContract(
                event_types=(
                    "vendor_user.provisioned",
                    "vendor_user.revoked",
                    "vendor_user.profile_updated",
                    "vendor_user.role_changed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and carries vendor-user, "
                    "field-vendor, principal, and role identity. It never "
                    "carries a credential."
                ),
                replay=(
                    "FieldVendorUser and its SystemUser rebuild membership "
                    "and capability; the credential is never replayed."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="auth.vendor_user_provisioning",
                verification=(
                    "Provisioning, identity-collision, inactive-vendor, "
                    "role, revocation, and route-capability tests."
                ),
            ),
            steward="vendor operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/test_vendor_identity.py",
                "tests/test_vendor_portal_auth.py",
            ),
        ),
    ),
)
