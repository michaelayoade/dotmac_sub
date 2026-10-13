"""Manual field identity authority, distinct from authentication and assignments."""

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

OWNER = "operations.field_identity"
CONCERN = "restricted field identity review lifecycle"
SERVICES = (
    SOTService(
        name=OWNER,
        module="app.services.field_identity",
        owns=(CONCERN, "sanitized approved field portrait disclosure"),
        depends_on=(
            "auth.permission_gate",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes="Manual independent staff review of exact private portrait/evidence revision. Authentication never verifies identity. Customer assignment owner must recheck assignment before every portrait download.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=CONCERN,
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated identity actor",
                        "restricted revision evidence",
                    ),
                    canonical_writer=OWNER,
                ),
                ConcernContract(
                    name="sanitized approved field portrait disclosure",
                    role=OwnerRole.POLICY,
                    input_names=("restricted revision evidence",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated identity actor",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="Active SystemUser and authenticated permission keys; independent reviewer requires operations:technician:write.",
                ),
                AuthorityInput(
                    name="restricted revision evidence",
                    owner=OWNER,
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Private StoredFile evidence and exact field_identity_revisions version. Portrait approval covers normalized metadata-free oriented PNG pixels; assignment-gated approved_portrait_stream re-encodes legacy originals under byte/pixel/dimension caps before disclosure. Evidence remains private and unchanged.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary="Exactly one execute_owner_command per submission or review; file metadata, receipt, audit and event staged atomically.",
                locking="Lock subject SystemUser before reads, creation, replay and review to serialize the first revision too.",
                idempotency="Unique subject/key receipt plus normalized intent fingerprint; authorization rechecked before replay.",
                retries="Retry whole command only on database serialization/deadlock; stable key required.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(
                    f"{OWNER}.{s}"
                    for s in (
                        "denied",
                        "not_found",
                        "stale_revision",
                        "invalid_request",
                        "idempotency_conflict",
                    )
                )
                + owner_command_boundary_error_codes(OWNER),
                mapping_owner="app.api.field.identity",
                fail_closed_on=(
                    "inactive subject or actor",
                    "self review",
                    "stale revision",
                    "conflicting replay",
                    "revoked or pending identity",
                    "malformed, animated, oversized or undecodable portrait",
                ),
            ),
            events=EventContract(
                event_types=("field_identity.changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Event carries opaque subject/revision/state only; no files or KYC.",
                replay="Receipt returns original outcome; live customer queries recheck current approval.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                new_owner=OWNER,
                old_owner="No prior field identity review owner",
                verification="Migration672 and focused lifecycle tests.",
                cutover_gate="Fresh/predecessor PostgreSQL migration rehearsal and authenticated identity tests pass.",
                fallback_retirement="No login-derived badge or public avatar fallback.",
            ),
            steward="field operations",
            design_refs=("docs/designs/FIELD_IDENTITY_REVIEW.md",),
            test_refs=(
                "tests/test_field_identity.py",
                "tests/test_field_identity_portrait_privacy.py",
                "tests/test_customer_field_identity.py",
            ),
        ),
    ),
)
