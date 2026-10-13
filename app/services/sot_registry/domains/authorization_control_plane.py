"""Canonical SOT declarations for the authorization_control_plane domain."""

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
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT


def _credential_administration_service(
    name: str,
    module: str,
    concern: str,
    *,
    coordinator: bool,
    events: tuple[str, ...],
    dependencies: tuple[str, ...],
) -> SOTService:
    input_name = f"{name} command and canonical state"
    customer_mfa = name == "auth.credential_administration"
    customer_cleanup = name == "auth.credential_administration_mutation"
    return SOTService(
        name=name,
        module=module,
        owns=(concern, "administrative customer MFA reset")
        if customer_mfa
        else (concern, "customer hard deletion auth cleanup")
        if customer_cleanup
        else (concern,),
        depends_on=dependencies,
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=concern,
                    role=OwnerRole.APPLICATION_COORDINATOR
                    if coordinator
                    else OwnerRole.COMMAND_WRITER,
                    input_names=(input_name,),
                    canonical_writer=None if coordinator else name,
                ),
            )
            + (
                (
                    ConcernContract(
                        name="administrative customer MFA reset",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(input_name,),
                    ),
                )
                if customer_mfa
                else ()
            )
            + (
                (
                    ConcernContract(
                        name="customer hard deletion auth cleanup",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(input_name,),
                        canonical_writer=name,
                    ),
                )
                if customer_cleanup
                else ()
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name=input_name,
                    owner=name,
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Typed authorized CommandContext and canonical principal, credential, registration metadata, and AuthSession rows; effective password/provider policy from control.settings_spec.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED
                if coordinator
                else TransactionMode.PARTICIPANT,
                boundary="Public coordinator executes once on a transaction-free session; participants flush only. Customer cleanup admits only the exact customer.account_deletion/delete_customer_account definition, matching context and AuditActor; sorted credential/MFA/session NOWAIT locks, no recovery/Party/binding removal. Credential, subscriber registration metadata, database session revocation, audit and outbox commit or roll back together. Preparation reads/hash occur on a separate session before locks.",
                locking="Principal FOR UPDATE precedes credential FOR UPDATE; creation and customer MFA reset serialize on refreshed Subscriber. MFA factors lock in UUID order before recovery and AuthSession updates. PostgreSQL per-lock wait is bounded at five seconds.",
                idempotency="No durable command replay claim. Repeated deactivation is a no-op; unique credential identity arbitrates creation. Password reset is a fresh transition on each successful invocation. Customer MFA clean repeat audits only; live-factor removal or secret/counter/recovery residue cleanup revokes sessions and stages one event.",
                retries="Lock timeout is retryable only after rollback and maps to 503 Retry-After. Unknown commit outcome is not automatically retried; inspect committed state before resubmission.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(
                    f"auth.credential_administration.{code}"
                    for code in (
                        "invalid_command_context",
                        "ineligible_customer",
                        "invalid_password",
                        "invalid_username",
                        "invalid_principal",
                        "principal_not_found",
                        "credential_not_found",
                        "radius_not_found",
                        "invalid_provider",
                        "stale_credential",
                        "conflict",
                        "lock_timeout",
                    )
                )
                + (owner_command_boundary_error_codes(name) if coordinator else ()),
                mapping_owner="app.api.auth and administrative web adapters",
                retryable_codes=("auth.credential_administration.lock_timeout",),
                fail_closed_on=(
                    "missing trusted actor or credential-write scope; customer MFA reset requires matching active SystemUser provenance and customer:write",
                    "active caller transaction including flushed writes",
                    "stale principal/provider",
                    "audit or outbox failure",
                ),
            ),
            events=EventContract(
                event_types=events,
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Identifiers, version, transition and command evidence only; never passwords, hashes or submitted secret values.",
                replay="Credential state and AuthSession revocation remain authoritative. Projection handler replay idempotently invalidates principal caches and portal projections.",
            ),
            projections=(
                ProjectionContract(
                    name="administrative credential session invalidation",
                    input_names=(input_name,),
                    writer="auth.credential_administration_mutation",
                    freshness="AuthSession revocation is atomic; cache/portal projection repair follows durable event dispatch.",
                    stale_behavior="Failed handler remains retryable. Redis portal revocation remains asynchronous until R-PR2 AuthSession-backed portal validation is deployed.",
                    drift_signal="Failed credential_session_projection handler attempt.",
                    rebuild_operation="Replay credential_administration.completed for the exact principal.",
                    repair_owner="auth.credential_administration",
                ),
            )
            if coordinator
            else (),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner=name,
                old_owner="auth.UserCredentials generic create/update/delete and web subscriber optional-login split commits",
                verification="Focused behavior and architecture tests plus PostgreSQL hosted concurrency tests.",
                cutover_gate="All credential API and optional-login writers call the coordinator; arbitrary hash/standing fields return 422.",
                fallback_retirement="Removed generic credential writers and local form hashing; no hash import HTTP interface.",
            ),
            steward="platform security",
            design_refs=(
                "docs/designs/CREDENTIAL_ADMINISTRATION.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_credential_administration.py",
                "tests/architecture/test_credential_administration_boundary.py",
                "tests/test_customer_mfa_reset.py",
            ),
        ),
    )


CREDENTIAL_ADMINISTRATION_SERVICES = (
    _credential_administration_service(
        "auth.credential_administration",
        "app.services.credential_administration",
        "administrative credential and subscriber login transition",
        coordinator=True,
        events=("credential_administration.completed", "subscriber.updated"),
        dependencies=(
            "auth.credential_administration_mutation",
            "customer.registration_metadata",
            "control.settings_spec",
            "events.dispatcher",
            "observability.audit_log",
        ),
    ),
    _credential_administration_service(
        "auth.credential_administration_mutation",
        "app.services.credential_administration_mutation",
        "administrative credential mutation",
        coordinator=False,
        events=("credential_administration.completed",),
        dependencies=("events.dispatcher", "observability.audit_log"),
    ),
    _credential_administration_service(
        "customer.registration_metadata",
        "app.services.customer_registration_metadata",
        "subscriber registration metadata mutation",
        coordinator=False,
        events=("subscriber.updated",),
        dependencies=("events.dispatcher", "observability.audit_log"),
    ),
)

PERMISSION_MATCHING_OWNER = "auth.permission_key_matching"
PERMISSION_MATCHING_CONCERN = "effective permission key vocabulary matching"
PERMISSION_MATCHING_SERVICE = SOTService(
    name=PERMISSION_MATCHING_OWNER,
    module="app.services.permission_matching",
    owns=(PERMISSION_MATCHING_CONCERN,),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name=PERMISSION_MATCHING_CONCERN,
                role=OwnerRole.POLICY,
                input_names=("effective permission matching inputs",),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="effective permission matching inputs",
                owner=PERMISSION_MATCHING_OWNER,
                kind=AuthorityKind.CONTROL_INPUT,
                source="Typed effective grant set and required permission; checked-in exact aliases and wildcard vocabulary. This policy does not load or grant permissions.",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.NOT_APPLICABLE,
            boundary="Pure matching with no database or external effects.",
            locking="None; immutable typed inputs.",
            idempotency="Same inputs return the same boolean.",
            retries="No retries; recompute after the grant owner refreshes effective grants.",
        ),
        errors=ErrorContract(
            domain_codes=(),
            mapping_owner="Calling authorization adapter",
            fail_closed_on=("No matching exact, explicit alias, or wildcard grant",),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner=PERMISSION_MATCHING_OWNER,
            verification="Existing RBAC wildcard tests and typed identity wildcard-review regression tests.",
            fallback_retirement="auth_dependencies reexports the original expansion helper; no parallel matching vocabulary remains there.",
        ),
        steward="identity and access",
        design_refs=("docs/designs/FIELD_IDENTITY_REVIEW.md",),
        test_refs=("tests/test_rbac_wildcard_perms.py", "tests/test_field_identity.py"),
    ),
)

STAFF_ADMISSION_OWNER = "auth.staff_permission_admission"
STAFF_ADMISSION_CONCERN = "fresh canonical active staff Person permission admission"
STAFF_ADMISSION_SERVICE = SOTService(
    name=STAFF_ADMISSION_OWNER,
    module="app.services.staff_permission_admission",
    owns=(STAFF_ADMISSION_CONCERN,),
    depends_on=("auth.permission_gate", "auth.erp_staff_access"),
    notes="Strict typed staff admission only. Delegates the gate-owned canonical RBAC calculator, then requires current active staff and canonical Person, native vendor exclusion and write leave eligibility. No route gate, generic permission policy, grants, denial audit or authentication mutation authority. Existing gate transport/audit debt remains outside this capability.",
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name=STAFF_ADMISSION_CONCERN,
                role=OwnerRole.POLICY,
                input_names=(
                    "server permission admission",
                    "canonical gate permission decision",
                    "canonical staff Person binding",
                    "canonical active Person",
                    "native vendor membership",
                    "current staff leave restriction",
                ),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="server permission admission",
                owner="auth.permission_gate",
                kind=AuthorityKind.CONTROL_INPUT,
                source="Typed server-produced PermissionAdmission from the existing canonical HTTP gate; never deserialized from user input.",
            ),
            AuthorityInput(
                name="canonical gate permission decision",
                owner="auth.permission_gate",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source="Typed PermissionPrincipal (native UUID/principal-kind enum/role and scope sets) passed to the gate-owned has_permission calculator. Current active Role and Permission rows and native staff role/direct grants are read fresh; exact aliases, wildcards and canonical admin semantics remain unchanged. The admission owner does not duplicate RBAC SQL or issue grants.",
            ),
            AuthorityInput(
                name="canonical staff Person binding",
                owner="auth.staff_provisioning",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Current SystemUser activity, system_user classification and canonical person_party_id binding; native UUIDs, never guessed from CRM identifiers.",
            ),
            AuthorityInput(
                name="canonical active Person",
                owner="party.registry",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Current Party row must be an active Person at the staff principal's exact canonical person_party_id; missing or inactive Person denies admission.",
            ),
            AuthorityInput(
                name="native vendor membership",
                owner="auth.vendor_user_provisioning",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Any native FieldVendorUser membership for the authentication principal excludes it from strict staff admission, independently of staff RBAC grants.",
            ),
            AuthorityInput(
                name="current staff leave restriction",
                owner="auth.erp_staff_access",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source="Typed active_leave_restriction query rechecks current effective ERP leave projection for write admission; read admission retains read-only access. Projection writes and denial audit remain with existing owners.",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.READ_ONLY,
            boundary="Read-only typed PermissionAdmission plus required permission/write mode to AuthorizedStaffPerson or stable domain refusal. Never completes caller transactions, writes grants, audits denials or mutates authentication state.",
            locking="Fresh server reads at invocation. A command caller revalidates after domain lock waits and compares returned Person with its initially evaluated Person; the final check defines authorization linearization, not a long-lived grant.",
            idempotency="No persisted state or receipts; repeat reads evaluate current canonical facts and may deny after revocation.",
            retries="No internal retries or stale/cache fallback. Calling owner retries its whole command only for database concurrency failure.",
        ),
        errors=ErrorContract(
            domain_codes=("auth.staff_permission_admission.denied",),
            mapping_owner="Calling command owner or HTTP adapter; no FastAPI in strict admission",
            fail_closed_on=(
                "non-staff principal",
                "missing exact server admission",
                "inactive principal or Person",
                "native vendor membership",
                "revoked current permission",
                "active staff leave write restriction",
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner=STAFF_ADMISSION_OWNER,
            verification="Typed boundary and fresh role/direct grant, wildcard, leave and caller post-lock revalidation unit tests.",
            cutover_gate="Engineering commands and protected current resolver use only this strict staff capability; coordinator full gates remain required.",
            fallback_retirement="No copied generic permission calculator, raw auth dictionary, Any result bag, route-gate migration, grant writer or new denial-audit path. Existing auth.permission_gate remains accurately in its legacy baseline.",
        ),
        steward="identity and access",
        design_refs=("docs/designs/PROJECT_ENGINEERING_PLANS.md",),
        test_refs=(
            "tests/test_project_engineering_plans.py",
            "tests/architecture/test_project_engineering_plan_boundary.py",
            "tests/test_rbac_wildcard_perms.py",
        ),
    ),
)

DOMAIN = DomainSOT(
    domain="authorization_control_plane",
    # `oidc` joins `local` here because this domain implements it: the
    # ceremony/exchange owner below is the verifier, and `AuthenticationBinding`
    # rows carry the code. Two issuers are two BINDINGS of this one code, which
    # is why installing another does not touch this declaration.
    authentication_mechanisms=("local", "oidc"),
    setting_domains=("auth",),
    services=(
        SOTService(
            name="auth.user_passwords",
            module="app.services.user_passwords",
            owns=(
                "user password representation verification",
                "explicit Argon2id hash parameters",
                "explicit native password admission",
                "dormant native password work observation",
            ),
            notes=(
                "User-only foundation with dormant explicitly configured process-local admission and work observation; no login installation or performance acceptance. "
                "Existing adapters default COMPAT; REFUSE "
                "is explicit and is not connected to settings. Service-secret compatibility "
                "remains in auth_flow. No rehash, notices, or persistence."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="user password representation verification",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "submitted user password and stored representation",
                            "explicit password policy and parameters",
                        ),
                    ),
                    ConcernContract(
                        name="explicit Argon2id hash parameters",
                        role=OwnerRole.POLICY,
                        input_names=("explicit password policy and parameters",),
                    ),
                    ConcernContract(
                        name="explicit native password admission",
                        role=OwnerRole.POLICY,
                        input_names=("explicit native password admission budget",),
                    ),
                    ConcernContract(
                        name="dormant native password work observation",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "submitted user password and stored representation",
                            "explicit password policy and parameters",
                            "explicit native password admission budget",
                            "explicit native password observer",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="explicit native password observer",
                        owner="auth.user_passwords",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Typed NativePasswordObserver receives secret-free low-level intervals after admission release and explicitly acknowledges capture; require_complete_capture rejects lost delivery after callers drain.",
                    ),
                    AuthorityInput(
                        name="submitted user password and stored representation",
                        owner="auth.user_passwords",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Typed UserPasswordVerification supplied by auth_flow; no database or secret-reference reads under REFUSE.",
                    ),
                    AuthorityInput(
                        name="explicit password policy and parameters",
                        owner="auth.user_passwords",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="ReversiblePasswordPolicy, Argon2idParameters and explicit Argon2idVerificationLimits; absent verification limits deny Argon2, COMPAT adapter default, no production parameter default.",
                    ),
                    AuthorityInput(
                        name="explicit native password admission budget",
                        owner="auth.user_passwords",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Immutable LimiterConfiguration explicitly supplies native capacity, bounded waiters, finite admission timeout, threadpool tokens and reserved tokens; no default or installed runtime caller.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary="Verification, hashing and process-local admission never write or complete a database transaction.",
                    locking="No database locks. A per-instance condition protects bounded active and waiting counters; no FIFO guarantee. Credential standing remains the authentication owner's responsibility.",
                    idempotency="Verification repeats the same decision; hashing intentionally generates a fresh random salt.",
                    retries="No automatic retries; compatibility secret-resolution failures retain their existing error boundary.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.user_passwords.invalid_argon2id_parameters",
                        "auth.user_passwords.invalid_admission_configuration",
                        "auth.user_passwords.admission_saturated",
                        "auth.user_passwords.admission_timed_out",
                        "auth.user_passwords.incomplete_native_capture",
                    ),
                    mapping_owner="auth_flow compatibility adapter and future password creation owners",
                    fail_closed_on=(
                        "malformed or unsupported hashes",
                        "REFUSE reversible material before resolution",
                        "bcrypt input above 72 UTF-8 bytes",
                        "invalid explicit admission budget",
                        "full waiter budget or expired admission timeout",
                        "incomplete native observation capture before measurement acceptance",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.user_passwords",
                ),
                steward="platform security",
                design_refs=("docs/designs/USER_PASSWORD_POLICY_FOUNDATION.md",),
                test_refs=(
                    "tests/test_user_password_policy.py",
                    "tests/test_user_password_admission.py",
                    "tests/test_user_password_work.py",
                    "tests/architecture/test_credential_verifier_boundary.py",
                    "tests/test_auth_legacy_bcrypt.py",
                ),
            ),
        ),
        SOTService(
            name="identity.capability_recipient",
            module="app.services.capability_recipient",
            owns=("exact authorized capability email recipient",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="exact authorized capability email recipient",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical capability contact field",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical capability contact field",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Contact email field supplied by the exact principal owner; parser must yield exactly one deliverable address.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary="Pure recipient resolution; no database mutation or transaction completion.",
                    locking="The calling principal owner supplies locked current contact state when requesting a capability.",
                    idempotency="Same contact value resolves the same one address or same refusal.",
                    retries="Malformed or multiple addresses require canonical contact correction before retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "identity.capability_recipient.no_deliverable_address",
                        "identity.capability_recipient.ambiguous_address",
                    ),
                    mapping_owner="Calling capability owner and authenticated adapters",
                    fail_closed_on=(
                        "no deliverable address",
                        "multiple deliverable addresses",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="identity.capability_recipient",
                ),
                steward="platform security",
                design_refs=("docs/EMAIL_VERIFICATION_EPHEMERAL_DELIVERY.md",),
                test_refs=("tests/test_capability_recipient.py",),
            ),
        ),
        SOTService(
            name="auth.email_verification",
            module="app.services.email_verification",
            owns=(
                "email verification delivery request",
                "email verification completion",
            ),
            depends_on=(
                "customer.accounts",
                "identity.capability_recipient",
                "auth.token_signing",
                "communications.intents",
                "communications.ephemeral_actions",
                "observability.audit_log",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="email verification delivery request",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("exact Subscriber verification request",),
                        canonical_writer="auth.email_verification",
                    ),
                    ConcernContract(
                        name="email verification completion",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "locked Subscriber email verification state",
                            "signed email capability",
                        ),
                        canonical_writer="auth.email_verification",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="exact Subscriber verification request",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Typed request context with trusted actual AuditActor and exact locked/reloaded Subscriber email, verified and active state; configured bounded TTL.",
                    ),
                    AuthorityInput(
                        name="locked Subscriber email verification state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Exact Subscriber UUID/current canonical email and verified flag, refreshed under the completion row lock; capability expiry is rechecked after lock acquisition, before mutation or no-op success.",
                    ),
                    AuthorityInput(
                        name="signed email capability",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Verified purpose, Subscriber principal UUID and exact email claim, decoded inside the completion owner; capability possession is not password login.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="Public request and completion each enter execute_owner_command once; completion stages the verified flag and canonical audit atomically. Flush-only stage_email_verification participates only in this owner or customer.portal_profile_commands. Audit and communication intent/outbox commit or roll back together.",
                    locking="Public request and completion set PostgreSQL transaction-local lock_timeout5s before exact Subscriber UUID FOR UPDATE with populate_existing; caller writes are never rolled back. Profile flush precedes participant reload.",
                    idempotency="Stable request key uses the existing unique intent dedupe constraint. Email-digest/Subscriber/TTL/purpose fingerprint mismatch fails closed; same evidence replays one intent. Completion is a no-op only for the same current verified address.",
                    retries="Retry complete request after rollback with unchanged context; unique constraint arbitrates cross-Subscriber key races, which fail without delivery.",
                ),
                errors=ErrorContract(
                    domain_codes=owner_command_boundary_error_codes(
                        "auth.email_verification"
                    )
                    + (
                        "auth.email_verification.invalid_command",
                        "auth.email_verification.invalid_capability",
                        "auth.email_verification.unavailable",
                        "auth.email_verification.active_caller_transaction",
                        "auth.email_verification.invalid_scope",
                        "auth.email_verification.invalid_participant",
                        "auth.email_verification.lock_timeout",
                        "auth.email_verification.idempotency_conflict",
                    ),
                    mapping_owner="Completion API/web map invalid capability to generic401/400 and operational failures503 Retry-After5. Authenticated API/customer/reseller resend adapters: lock_timeout maps to 503 Retry-After5; impersonation without actual actor is refused403",
                    retryable_codes=(
                        "auth.email_verification.lock_timeout",
                        "auth.email_verification.unavailable",
                    ),
                    fail_closed_on=(
                        "changed recipient or verified/inactive account",
                        "malformed purpose/audience/context",
                        "audit or intent queue failure",
                        "request fingerprint conflict",
                    ),
                ),
                events=EventContract(
                    event_types=("auth.email_verification",),
                    schema_version=1,
                    delivery_owner="communications.intents",
                    compatibility="Intent and Notification bodies are null. Allowlisted descriptor stores UUID, email digest, bounded TTL and purpose; request audit carries safe request IDs and outcome only. Completion stages canonical audit only, with proof kind and request UUIDs; no completion delivery event.",
                    replay="Worker revalidates exact current address and unverified state before minting and rendering solely in memory. Existing worker owns sensitive transport/outcomes.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner="auth_flow.send_email_verification; auth_flow.verify_email",
                    new_owner="auth.email_verification",
                    verification="Focused atomicity, replay, materialization and adapter isolation tests; PostgreSQL lock/unique tests require hosted migrated-schema gate.",
                    cutover_gate="API and portal/reseller resend plus portal profile changes use intent owner. API/web completion use the atomic capability owner; legacy completion writer is retired.",
                    fallback_retirement="Legacy mobile /me still calls set_subscriber_email and direct compatibility wrapper; retire only with reviewed partial-profile coordinator. S7 remains open; query-token URL remains until browser bridge is implemented.",
                ),
                steward="platform security",
                design_refs=(
                    "docs/EMAIL_VERIFICATION_EPHEMERAL_DELIVERY.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                ),
                test_refs=(
                    "tests/test_email_verification_intents.py",
                    "tests/test_email_verification_completion.py",
                    "tests/integration/test_email_verification_completion_pg.py",
                    "tests/integration/test_email_verification_intents_pg.py",
                ),
            ),
        ),
        SOTService(
            name="auth.api_key_administration",
            module="app.services.api_key_administration",
            owns=(
                "administrative JSON API-key revocation",
                "administrative JSON API-key issuance",
                "administrative JSON API-key update",
                "administrative web API-key creation revocation and rotation",
                "local API-key authentication maintenance",
                "customer hard deletion API-key cleanup",
            ),
            depends_on=("observability.audit_log",),
            notes=(
                "Named stage_staff_key_deletion is a typed flush-only participant of auth.staff_provisioning inactive deletion. It requires the active parent owner and exact actor/context, locks keys in UUID order and audits only actually deleted IDs; never executes or completes a transaction. Public API-key commands retain owner-managed mode."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer hard deletion API-key cleanup",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("exact customer deletion key set",),
                        canonical_writer="auth.api_key_administration",
                    ),
                    ConcernContract(
                        name="administrative JSON API-key revocation",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("authorized revocation and ApiKey",),
                        canonical_writer="auth.api_key_administration",
                    ),
                    ConcernContract(
                        name="administrative JSON API-key issuance",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("authorized issuance command",),
                        canonical_writer="auth.api_key_administration",
                    ),
                    ConcernContract(
                        name="administrative JSON API-key update",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("authorized PATCH command and ApiKey",),
                        canonical_writer="auth.api_key_administration",
                    ),
                    ConcernContract(
                        name="administrative web API-key creation revocation and rotation",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized web command and caller subscriber scope",
                        ),
                        canonical_writer="auth.api_key_administration",
                    ),
                    ConcernContract(
                        name="local API-key authentication maintenance",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("presented local API key and current standing",),
                        canonical_writer="auth.api_key_administration",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="exact customer deletion key set",
                        owner="customer.account_deletion",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Exact active delete_customer_account definition, matching trusted actor/context, UUID target and pre-parent locked key IDs; current subscriber binding rechecked in DELETE.",
                    ),
                    AuthorityInput(
                        name="authorized revocation and ApiKey",
                        owner="auth.api_key_administration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Immutable UUID target, trusted administrative AuditActor and matching CommandContext; refreshed locked ApiKey row.",
                    ),
                    AuthorityInput(
                        name="authorized issuance command",
                        owner="auth.api_key_administration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Frozen typed issuance command, trusted administrative actor and fixed provenance; subscriber existence and legacy field semantics.",
                    ),
                    AuthorityInput(
                        name="authorized PATCH command and ApiKey",
                        owner="auth.api_key_administration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Frozen typed PATCH command with exact selected field enum set, fixed administrative provenance and matching context; refreshed locked target, existing subscriber existence policy.",
                    ),
                    AuthorityInput(
                        name="authorized web command and caller subscriber scope",
                        owner="auth.api_key_administration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Typed command and trusted system-route actor context; explicit caller subscriber scope, legacy cached auth caps and refreshed locked target.",
                    ),
                    AuthorityInput(
                        name="presented local API key and current standing",
                        owner="auth.api_key_administration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Frozen raw-key command with fixed presented-key provenance and GENERAL/AUDIT audience; owner hashes candidates and verifies scalar current ApiKey standing. No claimed key ID or actor input.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="authenticate enters once on a fresh session: hot snapshot has no writes; cold exact-version CAS maintains hash/timestamp and canonical hash-upgrade audit atomically, with post-update refusal rolling back. create/generate/update/revoke/web_create/web_mutate each enter execute_owner_command once on a fresh session; ApiKey and canonical AuditEvent commit or roll back together. Generation rate admission precedes the command. No external effects. stage_customer_key_deletion is flush-only and admitted under the exact customer.account_deletion command; deletes only captured locked IDs still bound to target, never reacquires keys after parent. Named stage_staff_key_deletion joins only the active auth.staff_provisioning deletion transaction and flushes API-key deletion plus per-key canonical audit; it does not enter the executor.",
                    locking="Authentication uses scalar reads and a single same-ID/hash/standing CAS UPDATE; zero rows permits exactly one fresh same-ID/candidate verification read. No row lock or update on hot authentication. Maintenance has five-second lock_timeout, wall-clock expiry and a timestamp CASE preserving newer touches. Revocation and PATCH: ApiKey FOR UPDATE/populate_existing and PostgreSQL five-second lock timeout. Issuance: new row INSERT, unique hash and subscriber FK backstops; no existing-key lock. Web create: plain subscriber existence lookup and FK backstop; legacy cached cap checks are best-effort and race across concurrent creators or JSON updates. No cap lock or global cap guarantee. Web mutation: exact refreshed key FOR UPDATE, no principal/FK change.",
                    idempotency="Authentication accepts current snapshots; HMAC upgrade is one-time, timestamp touch at most once per five-minute window. CAS misses never retry maintenance or grant stale projections. Revocation stamps fresh state/audit each invocation. Issuance creates a new key; generated material is returned once after commit. PATCH applies supplied fields and audits every successful invocation, including empty/no-op payloads. Web creation creates a new key; rotation replaces the secret once after commit, repeated revocation stamps again. Missing/out-of-scope targets and inactive/revoked rotation return unchanged outcomes without audit. No durable replay or retry claim.",
                    retries="No automatic retry; lock timeout maps to 503 Retry-After 5 after rollback. Inspect state after unknown commit outcome.",
                ),
                errors=ErrorContract(
                    domain_codes=owner_command_boundary_error_codes(
                        "auth.api_key_administration"
                    )
                    + (
                        "auth.api_key_administration.invalid_authentication_command",
                        "auth.api_key_administration.authentication_busy",
                        "auth.api_key_administration.authentication_unavailable",
                        "auth.api_key_administration.invalid_participant",
                        "auth.api_key_administration.invalid_actor",
                        "auth.api_key_administration.not_found",
                        "auth.api_key_administration.invalid_command",
                        "auth.api_key_administration.subscriber_not_found",
                        "auth.api_key_administration.storage_unavailable",
                        "auth.api_key_administration.busy",
                        "auth.api_key_administration.limit_reached",
                        "auth.api_key_administration.ttl_exceeded",
                        "auth.api_key_administration.invalid_scope",
                    ),
                    mapping_owner="app.services.auth_dependencies authentication adapter, app.api.auth JSON API-key routes and app.web.admin.system web API-key routes",
                    fail_closed_on=(
                        "missing trusted actor",
                        "active caller transaction",
                        "canonical audit failure",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "auth.api_key.revoked",
                        "auth.api_key.created",
                        "auth.api_key.generated",
                        "auth.api_key.updated",
                        "auth.api_key.rotated",
                        "auth.api_key.deleted",
                        "auth.api_key.hash_upgraded",
                    ),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility="Hash upgrade audit contains verified key actor/target IDs, scheme names, fixed scope/reason and command IDs, never hash/raw/label/scope values; timestamp-only maintenance has no per-auth audit. Canonical AuditEvent only: target/actor identifiers, old/new active/revoked state for JSON revocation, active state only for web mutations and issuance, allowlisted requested field names/hash-change boolean for PATCH and command/correlation identifiers. No external event or secret payload.",
                    replay="Audit records are evidence, not delivery commands; no external dispatch or automatic replay. Database immutability is outside this slice.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="auth.api_key_administration",
                    old_owner="auth_dependencies last-used/hash-upgrade ORM helpers, auth.ApiKeys JSON writers and web_system_api_key_forms/create and web_system_api_key_mutations",
                    verification="Focused typed behavior, transaction trace and architecture tests; migrated PostgreSQL locking acceptance is hosted-only.",
                    cutover_gate="Both local authentication branches consume immutable owner projections through fresh command sessions; machine-first general auth remains outside. JSON POST create/generate, PATCH and DELETE use fresh owner command sessions and existing route-local admin guards. Generation preserves Redis admission ordering. Web POST create/revoke/rotate retain inherited admin and route settings-write guards, typed trusted actor context, caller scope and one-time POST rendering.",
                    fallback_retirement="Legacy issuance/update/revocation writers removed after repository-wide consumer search; limiter retained as admission-only helper. Web writer helpers retired after all consumers migrated; Inactive staff key deletion now delegates to the named participant; customer deletion remains outside this slice. Both legacy authentication mutation helpers are retired with no fallback.",
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/API_KEY_REVOCATION.md",
                    "docs/designs/API_KEY_AUTHENTICATION_MAINTENANCE.md",
                ),
                test_refs=(
                    "tests/test_api_key_authentication_maintenance.py",
                    "tests/integration/test_api_key_authentication_maintenance_pg.py",
                    "tests/test_staff_account_deletion.py",
                    "tests/integration/test_staff_account_deletion_pg.py",
                    "tests/test_api_key_administration.py",
                    "tests/test_api_key_issuance.py",
                    "tests/test_api_key_update.py",
                    "tests/test_web_api_key_administration.py",
                    "tests/integration/test_web_api_key_administration_pg.py",
                    "tests/integration/test_api_key_revocation_concurrency.py",
                    "tests/integration/test_api_key_issuance_pg.py",
                    "tests/integration/test_api_key_update_pg.py",
                    "tests/architecture/test_api_key_administration_boundary.py",
                ),
            ),
        ),
        *CREDENTIAL_ADMINISTRATION_SERVICES,
        PERMISSION_MATCHING_SERVICE,
        STAFF_ADMISSION_SERVICE,
        SOTService(
            name="auth.credential_security_inventory",
            module="app.services.credential_security_inventory",
            owns=(
                "credential security aggregate census",
                "reversible credential recovery prerequisite aggregate",
            ),
            depends_on=("auth.password_authentication", "auth.credential_recovery"),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="credential security aggregate census",
                        role=OwnerRole.RESOLVER,
                        input_names=("stored user credential security state",),
                    ),
                    ConcernContract(
                        name="reversible credential recovery prerequisite aggregate",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "stored user credential security state",
                            "exact recovery prerequisite interpretation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="stored user credential security state",
                        owner="auth.password_authentication",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="user_credentials format prefixes, principal links, provider, activity and timestamps",
                    ),
                    AuthorityInput(
                        name="exact recovery prerequisite interpretation",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Typed recovery-owned SQL projection of exact local binding, principal activity/cancellation and contact presence; no delivery or support claim.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Adapter supplies a clean PostgreSQL REPEATABLE READ READ ONLY snapshot; no writes or transaction completion in query.",
                    locking="No row locks; one repeatable-read snapshot.",
                    idempotency="Same snapshot and UTC observation time produce the same bounded counts.",
                    retries="Retry failed reads with a fresh snapshot.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.credential_security_inventory.read_only_required",
                    ),
                    mapping_owner="credential_security_inventory CLI; fixed redacted error output",
                    fail_closed_on=(
                        "dirty session",
                        "non-PostgreSQL database",
                        "write-capable snapshot",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.credential_security_inventory",
                ),
                steward="platform security",
                design_refs=("docs/runbooks/CREDENTIAL_SECURITY_INVENTORY.md",),
                test_refs=(
                    "tests/test_credential_security_inventory.py",
                    "tests/test_credential_recovery_readiness.py",
                    "tests/integration/test_credential_recovery_readiness_snapshot.py",
                    "tests/architecture/test_credential_security_inventory_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.permission_gate",
            module="app.services.auth_dependencies",
            owns=(
                "route permission dependencies",
                "request principal permission checks",
            ),
            depends_on=(
                "auth.rbac_catalog",
                "auth.subscriber_assignments",
                "auth.permission_key_matching",
            ),
        ),
        SOTService(
            name="auth.customer_login_identity",
            module="app.services.customer_login_identity",
            owns=(
                "customer local-login identity resolution",
                "ambiguous customer email login refusal",
            ),
            depends_on=(
                "customer.accounts",
                "access.subscription_lifecycle",
                "auth.customer_credential_enrollment",
            ),
            notes=(
                "Credential username remains the canonical login identity. "
                "A case-insensitive customer contact email is a read-only "
                "alias only when it identifies one eligible Subscriber and "
                "one active local portal credential. Shared email and "
                "multiple-credential evidence fail closed without selecting "
                "an account. Suspended, blocked, delinquent, and new customer "
                "states retain their existing login eligibility; disabled, "
                "canceled, or inactive customers cannot be selected by email."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer local-login identity resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical customer contact and active state",
                            "canonical customer lifecycle state",
                            "canonical customer local credential state",
                        ),
                    ),
                    ConcernContract(
                        name="ambiguous customer email login refusal",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical customer contact and active state",
                            "canonical customer lifecycle state",
                            "canonical customer local credential state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical customer contact and active state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscribers.email and subscribers.is_active",
                    ),
                    AuthorityInput(
                        name="canonical customer lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscribers.status",
                    ),
                    AuthorityInput(
                        name="canonical customer local credential state",
                        owner="auth.customer_credential_enrollment",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "local user_credentials username, subscriber_id, "
                            "password presence, and active state"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Authentication adapters supply a Session; the resolver "
                        "performs no writes or transaction completion."
                    ),
                    locking=(
                        "No locks; one committed snapshot supplies customer and "
                        "credential eligibility."
                    ),
                    idempotency=(
                        "Repeated resolution over the same snapshot returns the "
                        "same typed match or refusal."
                    ),
                    retries="Callers may retry only with a fresh transaction snapshot.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.customer_login_identity.ambiguous_email",
                        "auth.customer_login_identity.inactive_credential",
                    ),
                    mapping_owner="customer authentication API and web adapters",
                    fail_closed_on=(
                        "shared eligible customer email",
                        "multiple active local credentials",
                        "inactive local credential",
                        "disabled, canceled, or inactive customer",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "direct UserCredential and Subscriber lookup in "
                        "app.services.auth_flow and app.services.web_customer_auth"
                    ),
                    new_owner="auth.customer_login_identity",
                    verification=(
                        "Focused resolver, API, browser-service, lockout, MFA, "
                        "session, and established identifier tests."
                    ),
                    cutover_gate=(
                        "Every customer local-login adapter delegates email "
                        "identity selection to the typed resolver."
                    ),
                    fallback_retirement=(
                        "Direct customer contact-email selection is removed from "
                        "authentication adapters and guarded by architecture tests."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/IDENTITY_EMAIL_DECOUPLING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_customer_login_identity.py",
                    "tests/test_auth_flow.py",
                    "tests/test_web_customer_auth.py",
                    "tests/architecture/test_customer_login_identity_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.subscriber_assignments",
            module="app.services.subscriber_assignments",
            owns=("subscriber role and direct-permission assignments",),
            depends_on=(
                "auth.rbac_catalog",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "This is the only application and seed writer for "
                "subscriber_roles and subscriber_permissions. Public "
                "commands own their complete transaction; reseller "
                "onboarding and seed workflows use only flush-only "
                "collaborators inside their coordinator transaction."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=("subscriber role and direct-permission assignments"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized subscriber assignment principal",
                            "active role and permission catalog",
                            "canonical subscriber assignment state",
                        ),
                        canonical_writer="auth.subscriber_assignments",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized subscriber assignment principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=("rbac:assign evidence carried in CommandContext"),
                    ),
                    AuthorityInput(
                        name="active role and permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("active roles and active UI-assignable permissions"),
                    ),
                    AuthorityInput(
                        name="canonical subscriber assignment state",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber_roles and subscriber_permissions",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public assignment command enters "
                        "execute_owner_command on a transaction-free session; "
                        "the grants, audit evidence, and versioned event commit "
                        "or roll back together. Reseller onboarding and seed "
                        "collaborators flush only."
                    ),
                    locking=(
                        "Target subscribers, active catalog references, and "
                        "existing grants are selected FOR UPDATE. Unique "
                        "constraints arbitrate concurrent duplicate grants."
                    ),
                    idempotency=(
                        "Duplicate grant and desired-state replacement "
                        "commands converge without parallel writes; adapter "
                        "intent keys are stored only as SHA-256 evidence."
                    ),
                    retries=(
                        "Adapters may retry a failed desired-state command "
                        "with the same intent key. Invalid scope, inactive "
                        "catalog, and conflict failures require changed input."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.subscriber_assignments.invalid_command",
                        "auth.subscriber_assignments.invalid_scope",
                        "auth.subscriber_assignments.subscriber_not_found",
                        "auth.subscriber_assignments.role_not_found",
                        "auth.subscriber_assignments.permission_not_found",
                        "auth.subscriber_assignments.role_grant_not_found",
                        "auth.subscriber_assignments.permission_grant_not_found",
                        "auth.subscriber_assignments.assignment_conflict",
                        "auth.subscriber_assignments.invalid_command_context",
                        "auth.subscriber_assignments.command_contract_violation",
                        "auth.subscriber_assignments.nested_owner_command",
                        "auth.subscriber_assignments.active_caller_transaction",
                        ("auth.subscriber_assignments.nested_transaction_completion"),
                    ),
                    mapping_owner=("app.api.rbac and app.web.admin.resellers"),
                    fail_closed_on=(
                        "missing rbac:assign evidence",
                        "inactive or non-assignable catalog references",
                        "invalid region or reseller grant scope",
                        "concurrent assignment conflicts",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("subscriber.assignments_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains subscriber, role, "
                        "scope, and permission identifiers but no PII or raw "
                        "idempotency key."
                    ),
                    replay=(
                        "Events are immutable assignment-change evidence. "
                        "Canonical assignment and catalog tables remain the "
                        "rebuild inputs."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.rbac subscriber assignment CRUD, "
                        "reseller onboarding role writes, and direct "
                        "scripts.seed.seed_rbac subscriber grant writes"
                    ),
                    new_owner="auth.subscriber_assignments",
                    verification=(
                        "Focused atomicity, scope, catalog-safety, API, "
                        "reseller, seed, cache, and architecture tests."
                    ),
                    cutover_gate=(
                        "Every application and seed subscriber assignment "
                        "write delegates to auth.subscriber_assignments."
                    ),
                    fallback_retirement=(
                        "The legacy app.services.rbac module and all direct "
                        "subscriber assignment writers are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_subscriber_assignments.py",
                    ("tests/architecture/test_subscriber_assignment_boundary.py"),
                ),
            ),
        ),
        SOTService(
            name="auth.rbac_catalog",
            module="app.services.rbac_catalog",
            owns=(
                "role catalog and role-permission policy",
                "permission catalog",
                "kernel Role identity projection",
            ),
            depends_on=(
                "tenancy.operator_tenant",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "This is the only application and seed writer for roles, "
                "permissions, and role_permissions. Catalog identities are "
                "case-normalized and protected by functional unique indexes. "
                "Permission identifiers have two or three letter-leading "
                "segments separated by colons or dots; segment characters are "
                "lowercase letters, numbers, underscores, and hyphens. The "
                "exact global wildcard retains its existing special case; "
                "identifier validation never grants access or creates aliases. "
                "Permission-policy updates preserve an unchanged legacy role "
                "name, while new and genuinely renamed roles must use the "
                "canonical lowercase identifier syntax. "
                "Migration 528 adds the nullable kernel Role identity on the "
                "same row; this owner alone writes the operator tenant and "
                "deterministic slug while roles.name remains authoritative. "
                "The service-level migration state is SHADOWING for that new "
                "projection only; established catalog command ownership remains "
                "complete. "
                "Assigned identities cannot be renamed or deactivated, and "
                "non-assignable permissions may be granted only to admin."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="role catalog and role-permission policy",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized RBAC catalog principal",
                            "canonical role and role-permission catalog",
                            "system-user role grant references",
                            "subscriber role grant references",
                        ),
                        canonical_writer="auth.rbac_catalog",
                    ),
                    ConcernContract(
                        name="permission catalog",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized RBAC catalog principal",
                            "canonical permission catalog",
                            "system-user permission grant references",
                            "subscriber permission grant references",
                        ),
                        canonical_writer="auth.rbac_catalog",
                    ),
                    ConcernContract(
                        name="kernel Role identity projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical role and role-permission catalog",
                            "operator tenant identity",
                            "deterministic role slug derivation policy",
                        ),
                        canonical_writer="auth.rbac_catalog",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized RBAC catalog principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac role/permission write or delete scope "
                            "evidence carried in CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical role and role-permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="roles and role_permissions",
                    ),
                    AuthorityInput(
                        name="canonical permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="permissions",
                    ),
                    AuthorityInput(
                        name="operator tenant identity",
                        owner="tenancy.operator_tenant",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="The provisioned deterministic Sub operator tenant",
                    ),
                    AuthorityInput(
                        name="deterministic role slug derivation policy",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "derive_role_slug and the typed collision report; no "
                            "counter or insertion-order disambiguation"
                        ),
                    ),
                    AuthorityInput(
                        name="system-user role grant references",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_user_roles references used by catalog safety policy",
                    ),
                    AuthorityInput(
                        name="subscriber role grant references",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber_roles references used by catalog safety policy",
                    ),
                    AuthorityInput(
                        name="system-user permission grant references",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "system_user_permissions references used by "
                            "catalog identity and deactivation safety policy"
                        ),
                    ),
                    AuthorityInput(
                        name="subscriber permission grant references",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "subscriber_permissions references used by "
                            "catalog identity and deactivation safety policy"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public catalog command enters "
                        "execute_owner_command on a transaction-free session; "
                        "the catalog row, nullable kernel identity projection, "
                        "complete role-permission policy, "
                        "audit evidence, and versioned event commit or roll "
                        "back together. Seed collaborators flush only."
                    ),
                    locking=(
                        "Existing catalog rows and relationship sets are "
                        "selected FOR UPDATE. Case-normalized PostgreSQL unique "
                        "indexes arbitrate concurrent natural-key creation, "
                        "kernel tenant/slug uniqueness arbitrates projection "
                        "collisions, "
                        "while grant-reference checks fail closed before rename "
                        "or deactivation."
                    ),
                    idempotency=(
                        "Role-permission replacement and seed convergence use "
                        "desired sets; duplicate grants are no-ops. Adapter "
                        "intent keys are stored only as SHA-256 evidence."
                    ),
                    retries=(
                        "Adapters may retry failed desired-state commands with "
                        "the same intent key. Validation, protected catalog, "
                        "and in-use failures require changed authoritative input."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.rbac_catalog.invalid_command",
                        "auth.rbac_catalog.invalid_role_name",
                        "auth.rbac_catalog.invalid_permission_key",
                        "auth.rbac_catalog.invalid_permissions",
                        "auth.rbac_catalog.role_not_found",
                        "auth.rbac_catalog.permission_not_found",
                        "auth.rbac_catalog.role_permission_not_found",
                        "auth.rbac_catalog.role_conflict",
                        "auth.rbac_catalog.permission_conflict",
                        "auth.rbac_catalog.catalog_conflict",
                        "auth.rbac_catalog.role_in_use",
                        "auth.rbac_catalog.permission_in_use",
                        "auth.rbac_catalog.protected_role",
                        "auth.rbac_catalog.protected_permission",
                        "auth.rbac_catalog.invalid_command_context",
                        "auth.rbac_catalog.command_contract_violation",
                        "auth.rbac_catalog.nested_owner_command",
                        "auth.rbac_catalog.active_caller_transaction",
                        "auth.rbac_catalog.nested_transaction_completion",
                    ),
                    mapping_owner=("app.api.rbac and app.web.admin.system"),
                    fail_closed_on=(
                        "missing catalog authorization evidence",
                        "case-normalized catalog collisions",
                        "kernel tenant/slug identity collisions",
                        "rename or deactivation of assigned identities",
                        "protected admin role or permission changes",
                        "non-assignable permission grants outside admin",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "rbac.role_catalog_changed",
                        "rbac.permission_catalog_changed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains authorization "
                        "identifiers but no PII or raw idempotency key."
                    ),
                    replay=(
                        "Events are immutable policy-change evidence. Canonical "
                        "catalog tables and checked-in seed desired sets remain "
                        "the rebuild inputs."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="Role kernel identity projection",
                        input_names=(
                            "canonical role and role-permission catalog",
                            "operator tenant identity",
                            "deterministic role slug derivation policy",
                        ),
                        writer="auth.rbac_catalog",
                        freshness=(
                            "Written on every canonical role mutation in R1; legacy "
                            "rows remain unprojected until touched or reviewed later."
                        ),
                        stale_behavior=(
                            "roles.name remains authorization authority. A NULL, "
                            "partial, colliding or mismatched projection blocks "
                            "kernel reader and lineage cutover."
                        ),
                        drift_signal=(
                            "ck_roles_kernel_identity_projection, kernel composite "
                            "unique keys, sole-writer architecture guard and the "
                            "typed role-slug collision report"
                        ),
                        rebuild_operation=(
                            "Re-run a separately reviewed future role-adoption "
                            "command through auth.rbac_catalog; R1 has no backfill."
                        ),
                        repair_owner="auth.rbac_catalog",
                    ),
                ),
                migration=MigrationContract(
                    # Role/permission command ownership was already complete.
                    # The service's newest authority expansion is the kernel
                    # identity projection, and that migration is only shadowing:
                    # it is nullable, unpopulated for untouched rows and unread.
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "roles.name-only global identity read by every existing "
                        "authorization path"
                    ),
                    new_owner="auth.rbac_catalog",
                    verification=(
                        "Typed deterministic slug and collision reports, sole-writer "
                        "guard, dual-write behavior, and PostgreSQL predecessor/fresh "
                        "migration canaries."
                    ),
                    cutover_gate=(
                        "The reviewed collision and mismatch cohorts are zero, every "
                        "role has a complete projection, kernel grant semantics have "
                        "shadow parity, and the atomic revision-0001 rehearsal passes."
                    ),
                    fallback_retirement=(
                        "R1 keeps roles.name authoritative and the kernel identity "
                        "nullable and unread; legacy identity reads retire only in a "
                        "later approved cutover."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/PLATFORM_ADOPTION_LEDGER.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_rbac_catalog_owner.py",
                    "tests/test_roles_r1_kernel_identity.py",
                    "tests/test_roles_r1_migration.py",
                    "tests/integration/test_roles_r1_migration.py",
                    "tests/architecture/test_rbac_catalog_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.system_user_assignments",
            module="app.services.system_user_assignments",
            owns=(
                "system-user role and direct-permission assignments",
                "source-scoped managed system-user role convergence",
            ),
            depends_on=(
                "auth.rbac_catalog",
                "auth.permission_gate",
                "events.dispatcher",
                "observability.audit_log",
                "party.registry",
            ),
            notes=(
                "This is the only application writer for system_user_roles and "
                "system_user_permissions. Local replacement preserves grants "
                "owned by ERP or another source. Every role convergence locks "
                "the active admin role before checking the final-active-admin "
                "invariant. Public administrative replacement owns its complete "
                "transaction; staff provisioning uses only flush-only "
                "source-scoped collaborators inside its coordinator transaction."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=("system-user role and direct-permission assignments"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized system-user assignment principal",
                            "active role and permission catalog",
                            "canonical system-user assignment state",
                            "canonical staff Party binding",
                        ),
                        canonical_writer="auth.system_user_assignments",
                    ),
                    ConcernContract(
                        name=("source-scoped managed system-user role convergence"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "active role and permission catalog",
                            "canonical system-user assignment state",
                        ),
                        canonical_writer="auth.system_user_assignments",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized system-user assignment principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac:assign authorization evidence in the typed "
                            "CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="active role and permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("active roles and active UI-assignable permissions"),
                    ),
                    AuthorityInput(
                        name="canonical system-user assignment state",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_user_roles and system_user_permissions",
                    ),
                    AuthorityInput(
                        name="canonical staff Party binding",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the reviewed SystemUser.person_party_id projection; "
                            "names and email addresses are never identity evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "The public replacement command enters "
                        "execute_owner_command on a transaction-free session; "
                        "roles, direct permissions, audit, and event evidence "
                        "commit or roll back together. Audit actor enrichment reads "
                        "the canonical staff Party binding in that transaction. "
                        "Collaborator methods "
                        "flush but never complete a coordinator transaction. Named remove_all_for_system_user requires typed removal input and exact active auth.staff_provisioning context; it preserves the final-active-admin guard and flushes grant cleanup only."
                    ),
                    locking=(
                        "The target principal and existing grants are selected "
                        "FOR UPDATE. Every role change locks the active admin "
                        "role row before evaluating the final-active-admin "
                        "invariant, serializing competing removals and disables."
                    ),
                    idempotency=(
                        "Each source converges only its own global role grants; "
                        "direct permissions converge to the requested set. "
                        "Repeated desired state is a no-op and adapters carry a "
                        "stable intent key recorded only as a digest."
                    ),
                    retries=(
                        "Adapters may retry failed commands with the same intent "
                        "key. Validation and final-admin failures are not "
                        "retryable without a changed authoritative input."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.system_user_assignments.invalid_command",
                        "auth.system_user_assignments.invalid_roles",
                        "auth.system_user_assignments.invalid_permissions",
                        "auth.system_user_assignments.system_user_not_found",
                        "auth.system_user_assignments.last_admin_required",
                        "auth.system_user_assignments.invalid_command_context",
                        "auth.system_user_assignments.command_contract_violation",
                        "auth.system_user_assignments.nested_owner_command",
                        "auth.system_user_assignments.active_caller_transaction",
                        "auth.system_user_assignments.nested_transaction_completion",
                    ),
                    mapping_owner="app.web.admin.system",
                    fail_closed_on=(
                        "missing assignment authorization evidence",
                        "inactive or unknown roles",
                        "inactive or non-assignable direct permissions",
                        "removal or deactivation of the final active admin",
                        "active caller transaction or nested completion",
                        "manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("system_user.assignments_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains identifiers and "
                        "authorization keys but no raw idempotency key or PII."
                    ),
                    replay=(
                        "Events are immutable decision evidence; authoritative "
                        "assignment tables remain repairable by replaying the "
                        "source-specific desired grant command."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_system_user_edit, "
                        "app.services.web_system_user_mutations, and legacy "
                        "app.services.rbac system-user grant helpers"
                    ),
                    new_owner="auth.system_user_assignments",
                    verification=(
                        "Focused atomicity, source preservation, final-admin, "
                        "adapter, and architecture boundary tests."
                    ),
                    cutover_gate=(
                        "All application-level system-user assignment writes "
                        "delegate to this owner and managed roles are read-only "
                        "in the local administrative editor."
                    ),
                    fallback_retirement=(
                        "Profile edits no longer write grants or active state; "
                        "legacy create and direct assignment mutation helpers "
                        "are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_system_user_assignments.py",
                    "tests/architecture/test_system_user_assignment_boundary.py",
                    "tests/architecture/test_audit_actor_provenance.py",
                ),
            ),
        ),
        SOTService(
            name="auth.entitlement_revocation",
            module="app.services.entitlement_revocation",
            owns=("session revocation for entitlement reductions",),
            depends_on=(
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Current login and refresh tokens omit roles and scopes and "
                "reload RBAC through require_user_auth, while that dependency "
                "still accepts compatibility tokens carrying embedded claims. "
                "Those claims remain valid until expiry and cannot be changed by "
                "cache invalidation. require_user_auth re-reads the authoritative "
                "sessions row on every request, so revoking that row is the one "
                "fail-closed next-request rule for both token forms. This owner "
                "revokes inside the reducing owner's "
                "transaction so revocation and reduction commit or roll back "
                "together, emits durable projection work, and registers strict "
                "cache invalidation for after commit — never before, or a "
                "concurrent read would repopulate the cache from uncommitted "
                "rows. A failed invalidation is counted and logged but cannot "
                "preserve authorization, because the database already denies. "
                "It does not decide whether a reduction occurred: that "
                "judgement belongs to the reducing owner, which alone knows the "
                "principal's effective access before and after."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="session revocation for entitlement reductions",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("reduced effective entitlement decision",),
                        canonical_writer="auth.entitlement_revocation",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="reduced effective entitlement decision",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "the reducing owner's before/after effective access "
                            "comparison, computed inside its own transaction"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "Runs inside the reducing owner's transaction and never "
                        "commits. Revocation and reduction commit or roll back "
                        "together; a half-applied pair would either strand a "
                        "live session on withdrawn access or log a principal "
                        "out for a change that was abandoned."
                    ),
                    locking=(
                        "Live sessions for the principal are selected FOR "
                        "UPDATE, serializing against a concurrent login or "
                        "refresh touching the same rows."
                    ),
                    idempotency=(
                        "Already-revoked and expired sessions are excluded, so "
                        "a replay revokes nothing further and preserves the "
                        "original revoked_at."
                    ),
                    retries=(
                        "Retry belongs to the reducing owner's command. The "
                        "post-commit cache invalidation is not retried inline; "
                        "the emitted event is the replay handle, and "
                        "authorization is already denied without it."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.entitlement_revocation.unknown_principal_type",
                    ),
                    mapping_owner="auth.system_user_assignments",
                    fail_closed_on=(
                        "auth.entitlement_revocation.unknown_principal_type",
                    ),
                ),
                events=EventContract(
                    event_types=("rbac.entitlement_reduction_revoked",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Additive payload only. Consumers must tolerate unknown "
                        "keys; revoked_session_ids is sorted and stable."
                    ),
                    replay=(
                        "Replayable as the record of a completed revocation. "
                        "Replay re-invalidates caches; it never re-grants."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.entitlement_revocation",
                    old_owner=None,
                    verification=(
                        "Canaries assert the revoked session no longer "
                        "satisfies the predicate require_user_auth applies, and "
                        "that widening, no-op and equivalent-regrant changes "
                        "revoke nothing."
                    ),
                ),
                steward="auth",
                design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
                test_refs=("tests/test_entitlement_revocation.py",),
            ),
        ),
        SOTService(
            name="auth.token_signing",
            module="app.services.auth_token_signing",
            owns=(
                "configured JWT signing key and algorithm resolution",
                "cryptographic signing and verification of typed capability envelopes",
            ),
            notes=(
                "Calling domain owners define token purpose, claims, lifetime, "
                "and authorization consequences. Auth owns only the signed "
                "envelope and never turns a domain capability into identity proof."
            ),
        ),
        SOTService(
            name="auth.access_invitations",
            module="app.services.access_invitations",
            owns=("access invitation lifecycle",),
            depends_on=(
                "auth.credential_recovery",
                "runtime.durable_timers",
                "events.dispatcher",
                "events.owner_outputs",
            ),
            notes=(
                "Records issued/accepted/expired/revoked evidence for the "
                "staff, reseller, user, and subscriber invitation "
                "capabilities, with a durable per-invitation expiry timer "
                "and a receipted expiry consumer. Rows are lifecycle "
                "evidence, never an access grant: the capability's "
                "redeem-time TTL check in the issuing domain remains the "
                "fail-closed gate, and a completed reset stamps "
                "acceptance."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="access invitation lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("issued invitation capabilities",),
                        canonical_writer="auth.access_invitations",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="issued invitation capabilities",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "exact reset capabilities minted for invite "
                            "purposes with their principal and TTL"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "record_issued participates in the caller's active "
                        "owner command or roots its own; the expiry "
                        "consumer enters execute_owner_command once on a "
                        "transaction-free session."
                    ),
                    locking=(
                        "Reissue supersedes the principal's prior issued "
                        "rows inside one transaction; expiry reloads the "
                        "row and state-guards the transition."
                    ),
                    idempotency=(
                        "Reissue replaces the expiry timer by generation; "
                        "consumer receipts make redelivery an exact no-op."
                    ),
                    retries=(
                        "A failed expiry consequence leaves no receipt; "
                        "the outbox redelivers until it commits."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.access_invitations.active_caller_transaction",
                        "auth.access_invitations.command_contract_violation",
                        "auth.access_invitations.invalid_command_context",
                        "auth.access_invitations.nested_owner_command",
                        "auth.access_invitations.nested_transaction_completion",
                    ),
                    mapping_owner="auth and admin web adapters",
                    fail_closed_on=("expiring an accepted or revoked invitation",),
                ),
                events=EventContract(
                    event_types=(
                        "access_invitation.issued",
                        "access_invitation.accepted",
                        "access_invitation.expired",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries invitation, principal, purpose, "
                        "and deadline identities; no email addresses, only "
                        "digests on the row."
                    ),
                    replay=(
                        "Invitation rows are the durable state; outputs are "
                        "evidence and consumer receipts reject replays."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "stateless capability TTLs with redeem-time-only "
                        "expiry and no lifecycle evidence"
                    ),
                    new_owner="auth.access_invitations",
                    verification=(
                        "Invitation lifecycle behavior tests and the "
                        "identity chain boundary test."
                    ),
                    cutover_gate=(
                        "Every invite issuance path records its invitation "
                        "and stages the expiry timer."
                    ),
                    fallback_retirement=(
                        "Redeem-time TTL checks are retained deliberately "
                        "as the fail-closed gate; no parallel lifecycle "
                        "writer exists."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/IDENTITY_ONBOARDING_CHAIN.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_access_invitations.py",
                    "tests/architecture/test_identity_onboarding_chain_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.credential_recovery",
            module="app.services.credential_recovery",
            owns=(
                "password recovery request and delivery intent",
                "password reset credential transition",
                "credential recovery session projection invalidation",
                "exact recovery prerequisite interpretation",
            ),
            depends_on=(
                "auth.token_signing",
                "communications.intents",
                "communications.ephemeral_actions",
                "control.settings_spec",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Recovery requests persist only PII-safe event context. "
                "The communication consequence revalidates an exact local "
                "principal and mints the bearer only at delivery time. "
                "Capability redemption is the only password-reset writer "
                "and atomically changes credentials, revokes database "
                "sessions, stages audit evidence, and emits an event."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="exact recovery prerequisite interpretation",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical recoverable principal state",),
                    ),
                    ConcernContract(
                        name="password recovery request and delivery intent",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential recovery command evidence",
                            "canonical recoverable principal state",
                            "credential recovery policy settings",
                            "durable recovery delivery boundary",
                        ),
                        canonical_writer="auth.credential_recovery",
                    ),
                    ConcernContract(
                        name="password reset credential transition",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential recovery command evidence",
                            "canonical recoverable principal state",
                            "credential recovery policy settings",
                            "verified recovery capability envelope",
                        ),
                        canonical_writer="auth.credential_recovery",
                    ),
                    ConcernContract(
                        name=("credential recovery session projection invalidation"),
                        role=OwnerRole.RECONCILER,
                        input_names=("canonical recoverable principal state",),
                        canonical_writer="auth.credential_recovery",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="credential recovery command evidence",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed CommandContext carrying public-auth or "
                            "authorized-administrator actor, scope, reason, "
                            "correlation, and idempotency evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical recoverable principal state",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Subscriber, SystemUser, or ResellerUser "
                            "identity and its active local user_credential, "
                            "password marker, auth_sessions, and any explicitly "
                            "required active reseller membership"
                        ),
                    ),
                    AuthorityInput(
                        name="credential recovery policy settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "declared password minimum and recovery lifetime "
                            "settings plus the owner-defined request rate policy"
                        ),
                    ),
                    AuthorityInput(
                        name="durable recovery delivery boundary",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "deduplicated communication intent and notification "
                            "outbox state created from the request event"
                        ),
                    ),
                    AuthorityInput(
                        name="verified recovery capability envelope",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "signature and expiry verified password_reset "
                            "claims minted for one exact principal"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public request or redemption enters "
                        "execute_owner_command on a transaction-free adapter "
                        "session. Request audit and outbox event, or credential "
                        "change, session revocation, audit, and completion "
                        "event commit or roll back together."
                    ),
                    locking=(
                        "Redemption selects the exact active principal and "
                        "local credential FOR UPDATE before comparing the "
                        "single-use password marker. Request rate limiting "
                        "precedes principal lookup."
                    ),
                    idempotency=(
                        "Each accepted request has one immutable event id and "
                        "its communication intent deduplicates on that id. A "
                        "redeemed capability is spent by password_updated_at, "
                        "so replay fails closed."
                    ),
                    retries=(
                        "Rolled-back commands may be retried with the same "
                        "intent evidence. Invalid or spent capabilities and "
                        "invalid passwords require changed input; event and "
                        "notification delivery retry independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.credential_recovery.invalid_command",
                        "auth.credential_recovery.invalid_password",
                        "auth.credential_recovery.invalid_reset_capability",
                        "auth.credential_recovery.credential_not_found",
                        ("auth.credential_recovery.invalid_command_context"),
                        ("auth.credential_recovery.command_contract_violation"),
                        "auth.credential_recovery.nested_owner_command",
                        "auth.credential_recovery.active_caller_transaction",
                        ("auth.credential_recovery.nested_transaction_completion"),
                    ),
                    mapping_owner=(
                        "app.api.auth_flow, app.services.web_auth, and portal "
                        "or administrative web adapters"
                    ),
                    fail_closed_on=(
                        "invalid, expired, or spent capability",
                        "principal or recipient drift",
                        "ambiguous subscriber identity in email-only recovery",
                        "principal outside an adapter-declared reseller membership",
                        "inactive or missing local credential",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "password_recovery.requested",
                        "password_recovery.completed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 contains identifiers, correlation evidence, "
                        "an email digest, and safe redirect context but never "
                        "raw email, password, hash, or bearer capability."
                    ),
                    replay=(
                        "Request-event replay converges on one communication "
                        "intent by event id. Completion events are immutable "
                        "evidence; credential state remains authoritative."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name=(
                            "recovery-invalidated authentication session projections"
                        ),
                        input_names=("canonical recoverable principal state",),
                        writer="auth.credential_recovery",
                        freshness=(
                            "Completion-event dispatch invalidates auth and "
                            "portal session projections immediately after the "
                            "credential transaction commits."
                        ),
                        stale_behavior=(
                            "The event handler attempt remains failed and "
                            "retriable; durable auth_sessions revocation stays "
                            "authoritative while projection repair is pending."
                        ),
                        drift_signal=(
                            "A failed credential-session projection handler "
                            "attempt on the password_recovery.completed event."
                        ),
                        rebuild_operation=(
                            "Replay password_recovery.completed for the exact "
                            "principal to idempotently invalidate auth cache and "
                            "revoke customer or reseller portal sessions."
                        ),
                        repair_owner="auth.credential_recovery",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.auth_flow forgot_password_flow, "
                        "request_password_reset, and reset_password plus "
                        "synchronous web and administrative email helpers"
                    ),
                    new_owner="auth.credential_recovery",
                    verification=(
                        "Focused request, materialization, redemption, replay, "
                        "session-revocation, adapter, and architecture tests."
                    ),
                    cutover_gate=(
                        "Public API, shared web, customer, reseller, admin, "
                        "staff-invite, and reseller-invite paths call only the "
                        "contracted owner or exact in-memory materializer."
                    ),
                    fallback_retirement=(
                        "Synchronous recovery email delivery, persisted bearer "
                        "content, adapter-owned credential mutation, service "
                        "HTTP exceptions, and service commits are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_credential_recovery.py",
                    "tests/architecture/test_credential_recovery_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.session_staging",
            module="app.services.auth_session_staging",
            owns=(
                "authentication session issuance staging",
                "committed authentication session token encoding",
            ),
            depends_on=(
                "auth.token_signing",
                "control.settings_spec",
                "party.staff_authentication_reader",
                "communications.team_inbox_routing",
                "observability.audit_log",
            ),
            notes=(
                "The typed flush-only participant inserts sessions, supersedes the same "
                "device and delegates staff presence inside the authenticating owner's "
                "transaction. It does not authenticate or complete transactions. "
                "Memory-only encoding consumes the committed signing snapshot; bearer "
                "and signing fields are excluded from repr and never enter evidence. "
                "AuthFlow retains thin observation adapters and legacy test compatibility "
                "issuance wrappers; their existence is not a whole-AuthFlow cutover or "
                "a claim that those wrappers stage caller audit. New product calls are "
                "restricted to password and OIDC coordinators by source inventory."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="authentication session issuance staging",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authenticated session issuance decision",
                            "canonical staff session context",
                            "session signing policy",
                            "session refresh lifetime",
                        ),
                        canonical_writer="auth.session_staging",
                    ),
                    ConcernContract(
                        name="committed authentication session token encoding",
                        role=OwnerRole.TRANSPORT,
                        input_names=("committed session signing snapshot",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated session issuance decision",
                        owner="external:auth_session_coordinator",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Typed exact principal UUID, optional staff Party/context pair, normalized client observations and expiry; calling password or OIDC owner retains current standing locks.",
                    ),
                    AuthorityInput(
                        name="canonical staff session context",
                        owner="party.staff_authentication_reader",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Party-led principal resolution rechecks the supplied staff context before any session mutation.",
                    ),
                    AuthorityInput(
                        name="session signing policy",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Existing AuthFlow policy adapters resolve process-held signing material, algorithm and access lifetime while the caller transaction is open; their legacy HTTP configuration-error wrapper remains explicit debt.",
                    ),
                    AuthorityInput(
                        name="session refresh lifetime",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Existing refresh lifetime resolver or the caller's explicit expiry.",
                    ),
                    AuthorityInput(
                        name="committed session signing snapshot",
                        owner="auth.session_staging",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Immutable in-memory StagedSession returned before owner completion and encoded only after the caller has committed.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary="Flush-only session insertion, same-device supersession and delegated staff presence participate in the calling owner transaction. Token encoding performs no database read or transaction completion.",
                    locking="Calling coordinators hold principal and credential standing locks before insertion; staff session foreign keys also acquire Party KEY SHARE. The participant does not release those locks or start another owner.",
                    idempotency="The caller owns command arbitration. Session insertion generates one fresh refresh credential; an aborted attempt rolls back insertion and device supersession together.",
                    retries="Only the caller retries the whole transition. Never retry insertion or encoding as an independent session owner.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.session_staging.invalid_command",
                        "staff_projection_missing",
                        "staff_party_has_no_principal",
                        "staff_party_owns_multiple_principals",
                        "staff_projection_conflict",
                    ),
                    mapping_owner="Password and OIDC calling owners and their adapters; staff discriminators are StaffProjectionError.refusal.value, not a fabricated code attribute.",
                    fail_closed_on=(
                        "invalid canonical session command",
                        "missing or conflicting staff Party/context pair",
                        "configuration, database or delegated presence failure",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "auth.login_succeeded",
                        "auth.oidc_mobile_assertion_admitted",
                    ),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility="These are actual caller-staged AuditEvent actions, not emissions by the participant. The password and OIDC coordinators stage their success evidence atomically with the session. The OIDC outbox event remains OIDC-owned. Legacy test wrappers provide no caller-audit guarantee.",
                    replay="Audit rows are durable evidence, not dispatcher delivery. Calling owners own retries and idempotency; a rolled-back transition leaves no successful session or success audit.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.session_staging",
                    verification="Focused session, password and OIDC regression tests plus source caller inventory; migrated PostgreSQL acceptance remains required.",
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/OIDC_MOBILE_FEDERATION.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_oidc_mobile_federation.py",
                    "tests/architecture/test_oidc_mobile_federation_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.password_authentication",
            module="app.services.password_authentication",
            owns=(
                "password authentication success transition",
                "MFA challenge completion",
                "authentication failure counters",
                "authenticated password change",
                "portal authentication source snapshots",
                "authenticated subscriber MFA enrollment",
                "principal-specific MFA profile projection",
            ),
            depends_on=(
                "auth.session_staging",
                "auth.token_signing",
                "app_sessions.auth",
                "party.staff_session_projection",
                "control.settings_spec",
                "observability.audit_log",
                "runtime.db_sessions",
                "events.dispatcher",
            ),
            notes=(
                "Credential standing is version-bound. Password verification "
                "and eligibility run read-only first; the success transition is "
                "ONE owner transaction whose first write is a conditional "
                "UPDATE on user_credentials.credential_version, so a reset that "
                "commits between verification and session issuance fails the "
                "gate instead of leaving an unrevoked session. MFA challenges "
                "carry the credential binding and re-run the gate. PPPoE "
                "(access credential) authentication never touches "
                "user_credentials. The hash-representation hook "
                "replace_password_representation is a dormant non-public participant "
                "accepting redacted PasswordRepresentationReplacement and returning "
                "PasswordRepresentationOutcome.REPLACED or CONFLICT. Its exact UUID, "
                "positive version and hash CAS changes only password_hash, preserves "
                "password_updated_at and credential_version, and disables autoflush "
                "throughout the transaction-local marker window. It is not called by "
                "login. UserCredential verification uses verify_user_password; "
                "AccessCredential verification uses verify_service_secret and "
                "retains its existing bcrypt refusal. Reversible user material "
                "remains compatible until the recovery route and audited "
                "refusal setting are delivered and approved for cutover. "
                "Customer portal local password and MFA use the same owner; "
                "Redis carries auth_session_id and database standing controls every request. "
                "Read-only portal source lookup returns immutable canonical RADIUS/access "
                "snapshots before network verification. Known-target denials are typed "
                "immutable decisions; failure counters "
                "and failed AuditEvent evidence commit together in a fresh independent "
                "owner-command session. Unknown targets produce aggregate telemetry only."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="principal-specific MFA profile projection",
                        role=OwnerRole.RESOLVER,
                        input_names=("credential standing record",),
                    ),
                    ConcernContract(
                        name="authenticated subscriber MFA enrollment",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authentication command evidence",
                            "credential standing record",
                            "canonical authenticating principal state",
                        ),
                        canonical_writer="auth.password_authentication",
                    ),
                    ConcernContract(
                        name="password authentication success transition",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authentication command evidence",
                            "credential standing record",
                            "canonical authenticating principal state",
                            "authentication policy settings",
                        ),
                        canonical_writer="auth.password_authentication",
                    ),
                    ConcernContract(
                        name="MFA challenge completion",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authentication command evidence",
                            "credential standing record",
                            "canonical authenticating principal state",
                            "verified MFA challenge envelope",
                        ),
                        canonical_writer="auth.password_authentication",
                    ),
                    ConcernContract(
                        name="authentication failure counters",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authentication command evidence",
                            "credential standing record",
                            "authentication policy settings",
                        ),
                        canonical_writer="auth.password_authentication",
                    ),
                    ConcernContract(
                        name="portal authentication source snapshots",
                        role=OwnerRole.RESOLVER,
                        input_names=("credential standing record",),
                    ),
                    ConcernContract(
                        name="authenticated password change",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authentication command evidence",
                            "credential standing record",
                            "authentication policy settings",
                        ),
                        canonical_writer="auth.password_authentication",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authentication command evidence",
                        owner="auth.password_authentication",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed CommandContext carrying the authenticating "
                            "principal as actor, the authentication scope and "
                            "a reason; denial evidence uses the distinct system actor "
                            "auth.password_authentication and never asserts that a "
                            "resolved target authenticated. Verified phase-A snapshots carry only "
                            "identifiers and the credential_version, never a "
                            "password, hash or token"
                        ),
                    ),
                    AuthorityInput(
                        name="credential standing record",
                        owner="auth.password_authentication",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "user_credentials row: credential_version, "
                            "is_active, provider, must_change_password, lock "
                            "window and failure counter; mfa_methods and "
                            "mfa_recovery_codes rows; access_credentials "
                            "active flag and update marker for PPPoE sources; "
                            "radius_users and linked access_credentials active flags, "
                            "subscriber/linkage and strict updated_at markers"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical authenticating principal state",
                        owner="party.staff_session_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "active subscriber, reseller user or Party-bound "
                            "staff principal re-read inside the gate "
                            "transaction after the principal key-share lock"
                        ),
                    ),
                    AuthorityInput(
                        name="authentication policy settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "failed-login and MFA attempt limits, lockout "
                            "minutes and password minimum length, resolved in "
                            "phase A before the owner transaction opens"
                        ),
                    ),
                    AuthorityInput(
                        name="verified MFA challenge envelope",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "signature and expiry verified version-2 mfa or "
                            "mfa_enrollment claims binding principal, audience, "
                            "source and credential id plus version"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public command enters execute_owner_command once "
                        "on a session that phase A has returned transaction-free. "
                        "The credential gate, session insert, presence, "
                        "recovery-code spend, method update and success audit "
                        "commit or roll back together. After a typed denial (and failed "
                        "owner rollback), failure evidence uses owner_command_session "
                        "on an independent engine connection, never the request session. "
                        "The optional atomic counter update and failed AuditEvent commit "
                        "or roll back together. Session-open, staging, flush, commit, close "
                        "and telemetry failures preserve the original denial; no counter-only fallback. "
                        "Pending and flushed request writes are never released to record failure. "
                        "The dormant representation replacement is a non-public participant "
                        "in its caller's transaction, never a command or transaction owner. "
                        "It performs no flush, commit, rollback or savepoint; pending unrelated "
                        "ORM writes stay pending until after its marker is cleared. Recoverable "
                        "client failures attempt marker cleanup with autoflush still disabled. "
                        "An aborted PostgreSQL transaction can refuse cleanup; the original "
                        "failure propagates unchanged to the owner for rollback."
                    ),
                    locking=(
                        "Authenticated subscriber enrollment locks refreshed Subscriber FOR UPDATE, "
                        "then exact authenticated AuthSession FOR UPDATE, revalidates active, "
                        "unrevoked, unexpired matching subscriber proof with no impersonation, "
                        "then locks all factors in UUID order. Reset uses the same principal "
                        "order; reset-first invalidates stale setup/confirmation sessions. "
                        "Subscriber enrollment never releases a caller transaction and uses "
                        "authentication:mfa:self as an internal proof scope, never RBAC. "
                        "No lock while hashing. PPPoE "
                        "gates take FOR SHARE on the access_credentials row "
                        "after the principal lock so a secret change cannot "
                        "commit mid-gate. A "
                        "session-minting command first takes FOR KEY SHARE on "
                        "the principal row (the lock its session foreign key "
                        "needs anyway), so its order matches reset: principal, "
                        "credential. The first write is then a conditional "
                        "UPDATE on the credential row; it waits on a concurrent "
                        "reset and PostgreSQL re-evaluates the predicate on the "
                        "new row version. Order: principal (key share), "
                        "credential, recovery codes, MFA method, sessions; forced "
                        "enrollment: principal, credential, MFA method, "
                        "recovery codes, sessions. "
                        "RADIUS sources then lock access_credentials followed by "
                        "radius_users, matching access-owner projection sync order. "
                        "Portal sessions expire at the initial portal TTL; Redis "
                        "refresh cannot extend database standing. Redis delivery "
                        "failure invokes best-effort compensating revocation. "
                        "lock_timeout is 5s and maps to a retryable refusal."
                    ),
                    idempotency=(
                        "A command is single-shot: a repeat re-verifies "
                        "against the current credential_version. A spent "
                        "recovery code or a changed credential fails the gate "
                        "and consumes nothing. Challenges are bound to "
                        "credential_version but not yet single use."
                    ),
                    retries=(
                        "A deadlock (40P01) retries the whole phase B once; "
                        "the gate re-evaluates the version so phase A stays "
                        "valid. Zero gate rows are never retried or re-verified. "
                        "Representation CAS loss returns CONFLICT without retry or a "
                        "secret, hash or version in the outcome. The caller owns all "
                        "database error rollback and retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.password_authentication.invalid_mfa_principal",
                        "auth.password_authentication.mfa_unavailable",
                        "auth.password_authentication.invalid_mfa_session",
                        "auth.password_authentication.invalid_mfa_method",
                        "auth.password_authentication.invalid_credentials",
                        "auth.password_authentication.locked",
                        "auth.password_authentication.must_change_password",
                        "auth.password_authentication.account_disabled",
                        "auth.password_authentication.admin_required",
                        "auth.password_authentication.invalid_mfa_token",
                        "auth.password_authentication.invalid_mfa_code",
                        "auth.password_authentication.mfa_locked",
                        "auth.password_authentication.credential_changed",
                        "auth.password_authentication.lock_timeout",
                        *owner_command_boundary_error_codes(
                            "auth.password_authentication"
                        ),
                    ),
                    mapping_owner=(
                        "app.services.auth_flow and the web or API "
                        "authentication adapters"
                    ),
                    retryable_codes=("auth.password_authentication.lock_timeout",),
                    fail_closed_on=(
                        "stale credential_version or changed credential standing",
                        "challenge without the version-2 credential binding",
                        "inactive or unresolvable principal at the gate",
                        "changed PPPoE secret during an MFA window",
                        "a failure-counter write error (never becomes success)",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "auth.login_succeeded",
                        "auth.password_step_succeeded",
                        "auth.password_changed",
                        "auth.authentication_denied",
                        "credential_administration.completed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Audit evidence is persisted by observability.audit_log in the owner transaction; "
                        "(schema_version 1 metadata). Subscriber self enrollment also stages "
                        "credential_administration.completed with safe principal/method IDs, "
                        "closed transition/provenance and invalidate_sessions=False; the "
                        "registered credential-session projection invalidates authentication "
                        "cache without revoking sessions. Metadata carries identifiers and the "
                        "credential_version for success only, never a secret, hash, token "
                        "or raw body. Failed evidence is an AuditEvent with is_success=False, "
                        "a bounded status, enum reason/surface/phase, canonical target UUID "
                        "and provenance, fresh attempt UUID, validated-or-generated correlation UUID "
                        "and normalized trusted client IP. No usernames, versions or Party enrichment. "
                        "Unknown, ambiguous and invalid unsigned identities produce label-free counters only. "
                        "EventStore/outbox is not a substitute for required audit evidence."
                    ),
                    replay=(
                        "Audit rows are immutable evidence; credential state "
                        "stays authoritative and is not rebuilt from them."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="subscriber MFA enrollment authentication cache",
                        input_names=("credential standing record",),
                        writer="auth.password_authentication",
                        freshness="Factor/audit/event intent commit atomically; credential_session_projection invalidates principal cache after commit.",
                        stale_behavior="AuthSession and factor rows stay authoritative; failed event handler remains retryable.",
                        drift_signal="Failed credential_session_projection handler attempt.",
                        rebuild_operation="Replay credential_administration.completed for the exact subscriber with invalidate_sessions=False.",
                        repair_owner="auth.password_authentication",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.auth_flow.AuthFlow.login, mfa_verify and "
                        "change_password with adapter-owned commits"
                    ),
                    new_owner="auth.password_authentication",
                    verification=(
                        "Unit gate, challenge and PPPoE-separation tests plus "
                        "PostgreSQL concurrency tests against a real database."
                    ),
                    cutover_gate=(
                        "AuthFlow login, MFA and password change call only the "
                        "owner for credential success, failure counters and "
                        "password writes. Customer local password/MFA use the same "
                        "owner and legacy unbound Redis sessions require re-login."
                    ),
                    fallback_retirement=(
                        "The read-modify-write failure counters, the "
                        "SELECT-then-update recovery-code spend and the "
                        "unbound MFA challenge are removed from AuthFlow and the "
                        "customer portal. Impersonation keeps existing authorization "
                        "and actor audit and now receives database standing; actor "
                        "session revocation propagation remains a separate follow-up."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/CUSTOMER_PORTAL_CREDENTIAL_STANDING.md",
                    "docs/security/AUTHENTICATION_DENIAL_AUDIT.md",
                    "docs/designs/USER_PASSWORD_POLICY_FOUNDATION.md",
                    "docs/designs/SUBSCRIBER_MFA_ENROLLMENT.md",
                ),
                test_refs=(
                    "tests/test_subscriber_mfa_enrollment.py",
                    "tests/integration/test_subscriber_mfa_enrollment.py",
                    "tests/architecture/test_subscriber_mfa_enrollment_boundary.py",
                    "tests/test_password_authentication.py",
                    "tests/test_authentication_denial_audit.py",
                    "tests/test_authentication_denial_adapters.py",
                    "tests/integration/test_authentication_denial_audit.py",
                    "tests/architecture/test_authentication_denial_boundary.py",
                    "tests/test_web_customer_auth.py",
                    "tests/architecture/test_password_authentication_boundary.py",
                    "tests/integration/test_password_auth_credential_race.py",
                ),
            ),
        ),
        SOTService(
            name="auth.customer_credential_enrollment",
            module="app.services.customer_credential_enrollment",
            owns=(
                "credential enrollment delivery request",
                "referral-created customer local credential enrollment",
                "credential enrollment capability purpose claims and lifetime",
                "single-use enrollment and account email verification consequence",
                "credential enrollment authentication cache projection",
            ),
            depends_on=(
                "auth.token_signing",
                "communications.intents",
                "customer.accounts",
                "referrals.account_conversion",
                "communications.ephemeral_actions",
                "control.settings_spec",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Creates no placeholder credential. The local credential and "
                "Subscriber email verification are committed together only "
                "after the emailed capability is redeemed. Party quarantine, "
                "Party contact verification, and account/subscription state "
                "remain with their existing owners."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="credential enrollment delivery request",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential enrollment command evidence",
                            "canonical referral account context",
                            "canonical customer credential state",
                            "credential enrollment policy settings",
                            "durable enrollment delivery intent",
                        ),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                    ConcernContract(
                        name=("referral-created customer local credential enrollment"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential enrollment command evidence",
                            "canonical referral account context",
                            "canonical customer credential state",
                            "credential enrollment policy settings",
                            "verified enrollment capability envelope",
                        ),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                    ConcernContract(
                        name=(
                            "credential enrollment capability purpose claims "
                            "and lifetime"
                        ),
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical referral account context",
                            "canonical customer credential state",
                            "credential enrollment policy settings",
                            "verified enrollment capability envelope",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "single-use enrollment and account email "
                            "verification consequence"
                        ),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential enrollment command evidence",
                            "canonical customer credential state",
                            "verified enrollment capability envelope",
                        ),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                    ConcernContract(
                        name=("credential enrollment authentication cache projection"),
                        role=OwnerRole.RECONCILER,
                        input_names=("canonical customer credential state",),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="credential enrollment command evidence",
                        owner="auth.customer_credential_enrollment",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed CommandContext carrying the public referral "
                            "or capability actor, scope, reason, command, "
                            "correlation, and idempotency evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical referral account context",
                        owner="referrals.account_conversion",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Referral, referred Party and Lead binding, "
                            "and the exact converted Subscriber identifier"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical customer credential state",
                        owner="auth.customer_credential_enrollment",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active converted Subscriber identity, email and "
                            "email_verified state plus its local user_credential"
                        ),
                    ),
                    AuthorityInput(
                        name="credential enrollment policy settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "database-authoritative password minimum, user invite "
                            "lifetime, credential enrollment request limit, and "
                            "request window settings"
                        ),
                    ),
                    AuthorityInput(
                        name="verified enrollment capability envelope",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "signature and expiry verified referral enrollment "
                            "claims for one exact referral, Party, Lead, "
                            "Subscriber, and email digest"
                        ),
                    ),
                    AuthorityInput(
                        name="durable enrollment delivery intent",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "referral-deduplicated communication intent and "
                            "notification outbox with a non-secret ephemeral "
                            "action descriptor"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each request or redemption enters "
                        "execute_owner_command on a transaction-free adapter "
                        "session. Request intent, audit, and event, or local "
                        "credential, Subscriber email verification, audit, and "
                        "completion event commit or roll back together."
                    ),
                    locking=(
                        "Requests and redemption lock the exact Referral, Lead, "
                        "and Subscriber in canonical order. Redemption rechecks "
                        "the absence of a local credential; the normalized local "
                        "username unique index arbitrates cross-principal races."
                    ),
                    idempotency=(
                        "A referral has one communication intent dedupe key. "
                        "Delivery retries remint the bearer from canonical "
                        "context. Credential existence spends every outstanding "
                        "capability, so replay fails closed."
                    ),
                    retries=(
                        "Rolled-back commands may retry after transient database "
                        "failures. Rate-limited requests return a typed outcome; "
                        "delivery and cache projection retry independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.customer_credential_enrollment.invalid_command",
                        ("auth.customer_credential_enrollment.invalid_configuration"),
                        "auth.customer_credential_enrollment.context_not_found",
                        "auth.customer_credential_enrollment.stale_context",
                        "auth.customer_credential_enrollment.inactive_account",
                        "auth.customer_credential_enrollment.invalid_capability",
                        "auth.customer_credential_enrollment.invalid_password",
                        "auth.customer_credential_enrollment.invalid_username",
                        ("auth.customer_credential_enrollment.username_unavailable"),
                        ("auth.customer_credential_enrollment.invalid_command_context"),
                        (
                            "auth.customer_credential_enrollment."
                            "command_contract_violation"
                        ),
                        ("auth.customer_credential_enrollment.nested_owner_command"),
                        (
                            "auth.customer_credential_enrollment."
                            "active_caller_transaction"
                        ),
                        (
                            "auth.customer_credential_enrollment."
                            "nested_transaction_completion"
                        ),
                    ),
                    mapping_owner=(
                        "app.api.crm_referrals, app.api.auth_flow, and "
                        "app.services.web_customer_auth adapters"
                    ),
                    fail_closed_on=(
                        "invalid, expired, or spent capability",
                        "referral, Party, Lead, Subscriber, or recipient drift",
                        "inactive account or existing local credential",
                        "username collision",
                        "missing or invalid canonical policy configuration",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "customer_credential_enrollment.requested",
                        "customer_credential_enrollment.completed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 contains canonical identifiers, command and "
                        "correlation evidence, delivery outcome, and an email "
                        "digest but never raw email, password, hash, rendered "
                        "content, or bearer capability."
                    ),
                    replay=(
                        "Request replay converges on the referral-deduplicated "
                        "communication intent. Completion replay leaves the "
                        "existing credential authoritative and repairs its auth "
                        "cache projection idempotently."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="enrolled customer authentication cache",
                        input_names=("canonical customer credential state",),
                        writer="auth.customer_credential_enrollment",
                        freshness=(
                            "Completion-event dispatch invalidates the exact "
                            "subscriber authentication cache immediately after "
                            "the credential transaction commits."
                        ),
                        stale_behavior=(
                            "The handler attempt remains failed and retriable; "
                            "the committed credential and Subscriber email "
                            "verification remain authoritative."
                        ),
                        drift_signal=(
                            "A failed CredentialSessionProjectionHandler attempt "
                            "on customer_credential_enrollment.completed."
                        ),
                        rebuild_operation=(
                            "Replay customer_credential_enrollment.completed for "
                            "the exact subscriber to invalidate its auth cache."
                        ),
                        repair_owner="auth.customer_credential_enrollment",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "uncontracted request_referral_enrollment and "
                        "complete_referral_enrollment service functions with "
                        "helper commits, nested transactions, transport-coded "
                        "errors, and best-effort cache invalidation"
                    ),
                    new_owner="auth.customer_credential_enrollment",
                    verification=(
                        "Focused request, suppression, dedupe, materialization, "
                        "redemption, replay, drift, event, projection, adapter, "
                        "and architecture tests."
                    ),
                    cutover_gate=(
                        "Referral signup, public auth API, and customer portal "
                        "form submit only typed commands on transaction-free "
                        "sessions; materialization remains transport-only."
                    ),
                    fallback_retirement=(
                        "Service commits, savepoints, status-coded domain errors, "
                        "adapter keyword mutation calls, direct best-effort cache "
                        "invalidation, and duplicate delivery intents are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/REFERRAL_CREDENTIAL_ENROLLMENT.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_referral_credential_enrollment.py",
                    (
                        "tests/architecture/"
                        "test_customer_credential_enrollment_boundary.py"
                    ),
                ),
            ),
        ),
        SOTService(
            name="party.staff_authentication_reader",
            module="app.services.staff_party_authentication",
            owns=(
                "Party-keyed staff principal resolution",
                "staff authentication projection refusal",
            ),
            depends_on=(
                "party.registry",
                "party.staff_session_projection",
                "auth.staff_provisioning",
            ),
            notes=(
                "The single owner of staff principal resolution for "
                "authentication. Four consumers delegate: login, refresh, "
                "per-request session validation, and vendor login "
                "eligibility. Vendor ACCESS eligibility stays owned by the "
                "vendor module; only identity resolution moved here. "
                "resolve_staff_principal_by_party is the canonical "
                "primitive and the query direction is the contract: it "
                "starts at the Party and finds the principal, never the "
                "reverse. system_user_id is compared as the Sub-owned staff "
                "context assertion and never used to resolve, because "
                "resolving from it and checking Party afterwards would agree "
                "on healthy data while leaving the legacy key authoritative. "
                "Fails closed with typed refusals and no legacy fallback. "
                "The deploy-1 assertion-first resolver has been deleted: a "
                "staff session without party_id is unusable, while revoked and "
                "non-active historical rows remain preserved. Refresh resolves "
                "identity before token "
                "rotation, and every new staff session is minted from an "
                "explicit typed Party/context binding. Rollback floor is "
                "migration 534: never roll back below it, or new sessions "
                "mint without party_id."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="Party-keyed staff principal resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Person Party identity",
                            "credential Party authentication projection",
                            "staff session Party projection",
                            "canonical staff context state",
                        ),
                    ),
                    ConcernContract(
                        name="staff authentication projection refusal",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "credential Party authentication projection",
                            "staff session Party projection",
                            "canonical staff context state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="parties Person identity records",
                    ),
                    AuthorityInput(
                        name="credential Party authentication projection",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="user_credentials.party_id",
                    ),
                    AuthorityInput(
                        name="staff session Party projection",
                        owner="party.staff_session_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="sessions.party_id",
                    ),
                    AuthorityInput(
                        name="canonical staff context state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_users.person_party_id",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Authentication adapters supply a Session; the reader "
                        "performs no writes or transaction completion."
                    ),
                    locking="No locks; resolution reads one committed projection.",
                    idempotency="Repeated reads over one snapshot return one result.",
                    retries="Callers may retry only with a fresh transaction snapshot.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "staff_projection_missing",
                        "staff_party_has_no_principal",
                        "staff_party_owns_multiple_principals",
                        "staff_projection_conflict",
                    ),
                    mapping_owner="authentication adapters",
                    fail_closed_on=(
                        "missing Party projection",
                        "missing or ambiguous staff principal",
                        "Party and legacy assertion conflict",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner="direct credential and session system_user_id lookup",
                    new_owner="party.staff_authentication_reader",
                    verification=(
                        "Focused behavior, PostgreSQL projection, direction-sensitive "
                        "architecture, SOT contract tests, and the production "
                        "ratchet-readiness report."
                    ),
                    cutover_gate=(
                        "Every live staff session has an approved Party projection, "
                        "all readers require it, and the assertion-first bridge is "
                        "deleted."
                    ),
                    fallback_retirement=(
                        "The assertion-first compatibility bridge is deleted. "
                        "Rollback is limited to source "
                        "121e1592db795d339c1bc6279277797891d41064 at image digest "
                        "sha256:27b5324e765add48214b3668d39bb19557acbfac4c8a7edd"
                        "98a4fb22b6e0c19a, retaining sessions.party_id evidence and "
                        "never crossing migration 534."
                    ),
                ),
                steward="platform security",
                design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
                test_refs=(
                    "tests/test_staff_party_authentication.py",
                    "tests/integration/test_session_party_projection.py",
                    "tests/architecture/test_staff_party_authentication_owner.py",
                ),
            ),
        ),
        SOTService(
            name="auth.staff_provisioning",
            module="app.services.staff_provisioning",
            owns=(
                "staff account provisioning",
                "inactive staff account deletion",
                "staff identity bootstrap",
                "staff identity maintenance",
                "staff display identity resolution",
                "staff login identity resolution",
                "staff field technician profile binding",
            ),
            depends_on=(
                "auth.api_key_administration",
                "auth.rbac_catalog",
                "auth.system_user_assignments",
                "auth.permission_gate",
                "party.registry",
                "communications.intents",
                "communications.ephemeral_actions",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "ERP HR commands enter one verified coordinator transaction. "
                "Provisioning explicitly supports trusted reconciliation and "
                "a create-only reject policy that cannot mutate an existing "
                "identity. "
                "This owner writes staff identity and credential bootstrap, "
                "keeps the canonical staff email and the one local credential "
                "username aligned even while access is inactive, prepares "
                "credential recovery, resolves credential drift and recovery "
                "eligibility for adapters, and projects persisted staff display "
                "identity for historical ownership labels, "
                "creates and binds one Person Party for every new principal, "
                "delegates managed grants to auth.system_user_assignments, "
                "stages audit and "
                "versioned events atomically, and leaves invite delivery to a "
                "deduplicated communication consequence. Invite capabilities "
                "are minted only at transport time and are never stored in the "
                "event or notification outbox."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="inactive staff account deletion",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=("authorized inactive staff deletion request",),
                    ),
                    ConcernContract(
                        name="staff account provisioning",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "ERP HR staff lifecycle request",
                            "authorized RBAC assignment principal",
                            "active role catalog",
                            "managed role grant state",
                            "canonical staff identity and credential state",
                            "canonical Person Party identity",
                        ),
                    ),
                    ConcernContract(
                        name="staff identity bootstrap",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "ERP HR staff lifecycle request",
                            "canonical staff identity and credential state",
                            "canonical Person Party identity",
                        ),
                        canonical_writer="auth.staff_provisioning",
                    ),
                    ConcernContract(
                        name="staff identity maintenance",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authorized staff identity principal",
                            "canonical staff identity and credential state",
                            "staff-linked field technician profile",
                        ),
                    ),
                    ConcernContract(
                        name="staff field technician profile binding",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized staff identity principal",
                            "canonical staff identity and credential state",
                            "staff-linked field technician profile",
                        ),
                        canonical_writer="auth.staff_provisioning",
                    ),
                    ConcernContract(
                        name="staff display identity resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical staff identity and credential state",),
                    ),
                    ConcernContract(
                        name="staff login identity resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical staff identity and credential state",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized inactive staff deletion request",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Frozen single/bulk UUID command, trusted administrative actor and matching RBAC context; refreshed locked SystemUser inactive standing and existing linked-record FK blockers.",
                    ),
                    AuthorityInput(
                        name="ERP HR staff lifecycle request",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "typed provision, managed-role, and active-state "
                            "commands received by app.api.staff_sync"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized RBAC assignment principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac:assign authorization result carried in "
                            "CommandContext actor and scope evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized staff identity principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac:assign administrator evidence or an authenticated "
                            "profile:self principal targeting its own SystemUser"
                        ),
                    ),
                    AuthorityInput(
                        name="active role catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active roles",
                    ),
                    AuthorityInput(
                        name="managed role grant state",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "source-scoped rows in system_user_roles and the "
                            "final-active-admin invariant"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff identity and credential state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("system_users and staff-bound local user_credentials"),
                    ),
                    AuthorityInput(
                        name="staff-linked field technician profile",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "technician_profiles rows whose system_user_id/person_id "
                            "bind directly to the native staff SystemUser"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "new staff Person Party and complete "
                            "SystemUser.person_party_id binding evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Each public staff write enters execute_owner_command "
                        "on a transaction-free adapter session; identity, "
                        "credentials, profile and login-identity changes, RBAC "
                        "grants, session revocation, audit, "
                        "Person Party identity, and the outbox event commit "
                        "together before return. Inactive staff single/bulk deletion atomically deletes auth dependents, delegates API-key and RBAC cleanup to named flush-only participants, deletes principals and stages canonical audit; no deletion outbox/external effect."
                    ),
                    locking=(
                        "A PostgreSQL advisory transaction lock serializes "
                        "provisioning and identity maintenance by normalized "
                        "email in sorted old/new order; existing principals and "
                        "their local credentials "
                        "are selected FOR UPDATE, and database unique constraints "
                        "arbitrate identity and grant keys. Deletion locks all unique selected principals sorted UUID FOR UPDATE NOWAIT before credentials, MFA, sessions and API keys sorted UUID, then RBAC cleanup with one deletion-only admin Role NOWAIT query. Principal/Role contention refuses immediately to avoid in-flight grantor FK cycles; other child/key waits retain PostgreSQL five-second lock_timeout. No Subscriber, Party or email advisory lock."
                    ),
                    idempotency=(
                        "Email is the provision natural key; managed roles, active "
                        "state, and the local credential username converge to "
                        "canonical staff state under the reconcile policy. The "
                        "create-only policy rejects an existing natural key before "
                        "mutation. Its stable ERP command reference is retained as "
                        "Party external-reference provenance so an exact replay "
                        "returns the created principal without mutating it. Invite "
                        "expansion deduplicates on the immutable provisioning event "
                        "id. Deletion has no replay/retry guarantee: single missing is not found, active is blocked; bulk active/missing/duplicate occurrences are skipped and any late blocker rolls back the entire batch."
                    ),
                    retries=(
                        "Adapters may retry a failed request with the same "
                        "idempotency key. Domain validation is not retryable; "
                        "a concurrent identity change requires a fresh read and "
                        "reviewed retry; "
                        "event-store delivery retries consequences independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.staff_provisioning.invalid_command",
                        "auth.staff_provisioning.active_staff_deletion",
                        "auth.staff_provisioning.linked_records",
                        "auth.staff_provisioning.deletion_busy",
                        "auth.staff_provisioning.deletion_unavailable",
                        "auth.staff_provisioning.unknown_roles",
                        "auth.staff_provisioning.staff_account_not_found",
                        "auth.staff_provisioning.identity_conflict",
                        "auth.staff_provisioning.credential_not_found",
                        "auth.staff_provisioning.credential_ambiguous",
                        "auth.staff_provisioning.inactive_staff_account",
                        "auth.staff_provisioning.password_update_forbidden",
                        "auth.staff_provisioning.invalid_password",
                        "auth.staff_provisioning.stale_identity_evidence",
                        "auth.staff_provisioning.concurrent_identity_change",
                        "auth.system_user_assignments.last_admin_required",
                        "auth.staff_provisioning.invalid_command_context",
                        "auth.staff_provisioning.command_contract_violation",
                        "auth.staff_provisioning.nested_owner_command",
                        "auth.staff_provisioning.active_caller_transaction",
                        "auth.staff_provisioning.nested_transaction_completion",
                    ),
                    mapping_owner=(
                        "app.api.staff_sync, app.api.auth_flow, and "
                        "app.web.admin.system"
                    ),
                    fail_closed_on=(
                        "missing authorization evidence",
                        "unknown or inactive roles",
                        "identity conflict",
                        "missing or ambiguous local credential state",
                        "stale reviewed repair evidence",
                        "concurrent identity change",
                        "new principal without a complete Person Party binding",
                        "active caller transaction",
                        "nested command or transaction completion",
                        "manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "staff_account.provisioned",
                        "staff_account.roles_changed",
                        "staff_account.activated",
                        "staff_account.deactivated",
                        "staff_account.identity_changed",
                        "staff_account.credential_reconciled",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and PII-safe; breaking payload "
                        "changes require a new schema version. Hard deletion emits canonical AuditEvent only: principal/key IDs and command correlation, never email, display name or credential material. No external deletion dispatch."
                    ),
                    replay=(
                        "State events are immutable evidence. Staff invitation "
                        "intent expansion is idempotent by event_id and mints a "
                        "fresh short-lived capability only at delivery time."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_system_user_edit, "
                        "app.services.web_system_user_mutations, direct profile "
                        "writers, and the legacy multi-commit staff provisioning path"
                    ),
                    new_owner="auth.staff_provisioning",
                    verification=(
                        "Focused API, transaction, event, audit, RBAC, identity "
                        "reconciliation, recovery, and ephemeral-delivery tests "
                        "plus architecture guards."
                    ),
                    cutover_gate=(
                        "All staff-sync, administrative identity, self-profile, "
                        "activation, invitation, and password-recovery routes call "
                        "only typed owner commands for staff identity state. Inactive single/bulk deletion uses fresh owner sessions with inherited admin and route RBAC assignment guards."
                    ),
                    fallback_retirement=(
                        "Staff sync no longer calls web_system_user_mutations or "
                        "synchronous email delivery; web adapters no longer mutate "
                        "staff profile or local credential identity directly. Both legacy staff delete helpers and single route post-commit PII audit are retired; customer deletion remains a separate owner migration."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/STAFF_ACCOUNT_DELETION.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/ERP_WORKFORCE_ACCOUNT_PROVISIONING.md",
                    "docs/designs/STAFF_LOGIN_IDENTITY_RECONCILIATION.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_staff_account_deletion.py",
                    "tests/integration/test_staff_account_deletion_pg.py",
                    "tests/test_api_staff_sync.py",
                    "tests/test_staff_provisioning_owner.py",
                    "tests/test_staff_login_identity_admin.py",
                    "tests/test_staff_login_identity_reconciliation_script.py",
                    "tests/architecture/test_staff_provisioning_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.erp_staff_access",
            module="app.services.erp_staff_access",
            owns=(
                "ERP staff leave restriction projection",
                "ERP staff account-status projection",
                "ERP staff access reconciliation",
            ),
            depends_on=(
                "auth.staff_provisioning",
                "auth.permission_gate",
                "auth.system_user_assignments",
                "observability.audit_log",
            ),
            notes=(
                "ERP remains the external authority for leave and employee "
                "account status. Sub stores an idempotent local projection and "
                "enforces temporary read-only staff writes locally without "
                "rewriting permanent roles or permissions."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ERP staff leave restriction projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "signed ERP staff leave restriction event",
                            "canonical staff identity and credential state",
                        ),
                        canonical_writer="auth.erp_staff_access",
                    ),
                    ConcernContract(
                        name="ERP staff account-status projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "signed ERP staff account-status event",
                            "canonical staff identity and credential state",
                        ),
                        canonical_writer="auth.erp_staff_access",
                    ),
                    ConcernContract(
                        name="ERP staff access reconciliation",
                        role=OwnerRole.RECONCILER,
                        input_names=(
                            "authoritative ERP staff access snapshot",
                            "local ERP staff access projections",
                            "canonical staff identity and credential state",
                        ),
                        canonical_writer="auth.erp_staff_access",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="signed ERP staff leave restriction event",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "HMAC-verified flat staff.leave_restriction.v1 payload "
                            "with delivery id, restriction id, employee mapping, "
                            "organization timezone, effective bounds, status, "
                            "version, and updated_at"
                        ),
                    ),
                    AuthorityInput(
                        name="signed ERP staff account-status event",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "HMAC-verified flat staff.account_status.v1 payload "
                            "with delivery id, employee mapping, desired "
                            "status, version, updated_at, and reason evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="authoritative ERP staff access snapshot",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Current ERP staff leave/account-status snapshot "
                            "used by the 15-minute reconciliation repair loop; "
                            "historical replay is not a source of truth."
                        ),
                    ),
                    AuthorityInput(
                        name="local ERP staff access projections",
                        owner="auth.erp_staff_access",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "erp_staff_leave_restrictions and "
                            "erp_staff_account_status_projections"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff identity and credential state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_users and staff-bound user_credentials",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Webhook and reconciliation adapters enter one "
                        "execute_owner_command call on a transaction-free "
                        "session; projection, source-aware staff active-state "
                        "changes, credential/session closure, audit, and auth "
                        "cache invalidation commit atomically."
                    ),
                    locking=(
                        "Projection rows and the mapped SystemUser are selected "
                        "FOR UPDATE. Database uniqueness on source/restriction "
                        "and source/employee keys arbitrates concurrent delivery."
                    ),
                    idempotency=(
                        "Delivery idempotency is recorded by integration.inbox; "
                        "projection state additionally rejects older versions "
                        "and treats duplicate same-version state as a no-op."
                    ),
                    retries=(
                        "Transient adapter or database failure retries the whole "
                        "delivery after rollback. Mapping conflicts and "
                        "same-version divergent state fail closed for review."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.erp_staff_access.invalid_command",
                        "auth.erp_staff_access.mapping_not_found",
                        "auth.erp_staff_access.mapping_conflict",
                        "auth.erp_staff_access.version_conflict",
                        "auth.system_user_assignments.last_admin_required",
                        "auth.staff_provisioning.identity_conflict",
                        "auth.staff_provisioning.credential_ambiguous",
                        *owner_command_boundary_error_codes("auth.erp_staff_access"),
                    ),
                    mapping_owner="app.api.erp_staff_access_webhooks",
                    fail_closed_on=(
                        "invalid authorization evidence",
                        "missing or conflicting ERP-to-Selfcare mapping",
                        "same-version divergent state",
                        "final active admin deactivation",
                        "credential identity conflict",
                        "active caller transaction",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "staff.leave_restriction.received",
                        "staff.leave_restriction.updated",
                        "staff.leave_restriction.cancelled",
                        "staff.account_status.inactive",
                        "staff.account_status.active",
                    ),
                    schema_version=1,
                    delivery_owner="integration.inbox",
                    compatibility=(
                        "The local projection is additive and does not change "
                        "permanent role, permission, or API-key contracts."
                    ),
                    replay=(
                        "Inbox duplicate delivery returns the recorded "
                        "consequence; projection monotonic versions prevent "
                        "old events from restoring stale leave windows or "
                        "reactivating cancelled restrictions."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="ERP staff leave restriction projection",
                        input_names=("signed ERP staff leave restriction event",),
                        writer="auth.erp_staff_access",
                        freshness=(
                            "current after latest accepted ERP version or the next "
                            "15-minute reconciliation pass"
                        ),
                        stale_behavior="older versions are audited and ignored",
                        drift_signal="reconciliation snapshot differs by version",
                        rebuild_operation="reconcile_staff_access_snapshot",
                        repair_owner="auth.erp_staff_access",
                    ),
                    ProjectionContract(
                        name="ERP staff account-status projection",
                        input_names=("signed ERP staff account-status event",),
                        writer="auth.erp_staff_access",
                        freshness="current after latest accepted ERP version",
                        stale_behavior="older versions are audited and ignored",
                        drift_signal="reconciliation snapshot differs by version",
                        rebuild_operation="reconcile_staff_access_snapshot",
                        repair_owner="auth.erp_staff_access",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.erp_staff_access",
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/ERP_STAFF_ACCESS_INTEGRATION.md",
                ),
                test_refs=(
                    "tests/test_erp_staff_access.py",
                    "tests/test_erp_staff_access_webhook.py",
                    "tests/test_erp_staff_access_reconciliation.py",
                ),
            ),
        ),
        SOTService(
            name="auth.reseller_onboarding",
            module="app.services.reseller_onboarding",
            owns=(
                "reseller portal principal onboarding",
                "reseller portal access revocation",
                "reseller portal MFA maintenance",
            ),
            depends_on=(
                "customer.accounts",
                "auth.subscriber_assignments",
                "auth.permission_gate",
                "communications.intents",
                "communications.ephemeral_actions",
                "control.feature_registry",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Administrative reseller onboarding enters one verified "
                "coordinator transaction. Canonical reseller and fallback "
                "Subscriber initialization, portal identity and credential "
                "bootstrap, assignment-owner grants, audit, and events commit "
                "atomically. Customer and reseller contact emails may overlap; "
                "only the local credential username is globally unique. "
                "Invitations are deduplicated event consequences; "
                "reset capabilities are minted only at transport time for the "
                "exact principal and never persisted in the outbox. Access "
                "revocation preserves identity history while atomically "
                "deactivating the reseller link, first-class credential, and "
                "active authentication sessions."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="reseller portal principal onboarding",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authorized reseller onboarding principal",
                            "canonical reseller and subscriber account state",
                            "canonical subscriber assignment state",
                            "reseller principal cutover gate",
                            "canonical reseller onboarding state",
                        ),
                    ),
                    ConcernContract(
                        name="reseller portal access revocation",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authorized reseller onboarding principal",
                            "canonical reseller onboarding state",
                        ),
                    ),
                    ConcernContract(
                        name="reseller portal MFA maintenance",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authorized reseller onboarding principal",
                            "canonical reseller onboarding state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized reseller onboarding principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "reseller:write and, when needed, rbac:assign "
                            "evidence carried in correlated CommandContexts"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical reseller and subscriber account state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "resellers, subscribers, and transaction-neutral "
                            "canonical initialization collaborators"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscriber assignment state",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber_roles and active role catalog references",
                    ),
                    AuthorityInput(
                        name="reseller principal cutover gate",
                        owner="control.feature_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="reseller_user_principal_enabled feature setting",
                    ),
                    AuthorityInput(
                        name="canonical reseller onboarding state",
                        owner="auth.reseller_onboarding",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "reseller_users and reseller-bound local user_credentials"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Each public onboarding or access-revocation command enters "
                        "execute_owner_command on a transaction-free adapter "
                        "session; every record, grant, audit event, and outbox "
                        "event commits or rolls back together."
                    ),
                    locking=(
                        "Existing resellers and active role references are "
                        "selected FOR UPDATE. PostgreSQL advisory transaction "
                        "locks serialize normalized email and username keys, "
                        "with database constraints arbitrating remaining races. "
                        "Revocation locks the exact reseller and membership before "
                        "credential and session changes."
                    ),
                    idempotency=(
                        "Adapters carry stable intent keys as hashed evidence. "
                        "Identity conflicts fail closed, assignment grants "
                        "converge, and invite expansion deduplicates by event id."
                    ),
                    retries=(
                        "Adapters may retry a rolled-back command with the same "
                        "intent key. Validation and identity conflicts require "
                        "changed input; event delivery retries independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.reseller_onboarding.invalid_command",
                        ("auth.reseller_onboarding.assignment_authorization_required"),
                        "auth.reseller_onboarding.identity_conflict",
                        "auth.reseller_onboarding.reseller_not_found",
                        "auth.reseller_onboarding.inactive_reseller",
                        "auth.reseller_onboarding.portal_user_not_found",
                        "auth.reseller_onboarding.role_not_found",
                        "auth.reseller_onboarding.unsupported_role_target",
                        ("auth.reseller_onboarding.invalid_command_context"),
                        ("auth.reseller_onboarding.command_contract_violation"),
                        "auth.reseller_onboarding.nested_owner_command",
                        "auth.reseller_onboarding.active_caller_transaction",
                        ("auth.reseller_onboarding.nested_transaction_completion"),
                    ),
                    mapping_owner="app.web.admin.resellers",
                    fail_closed_on=(
                        "missing or mismatched authorization evidence",
                        "inactive reseller or role",
                        "identity collision",
                        "portal principal outside the requested reseller",
                        "unsupported first-class principal role assignment",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "reseller.created",
                        "reseller_user.provisioned",
                        "reseller_user.access_revoked",
                        "subscriber.created",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 onboarding events contain identifiers, "
                        "role names, and an email digest but no PII, password, "
                        "or reset capability."
                    ),
                    replay=(
                        "Events are immutable evidence. Invitation expansion "
                        "is idempotent by event id and revalidates the exact "
                        "canonical principal before minting a capability."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_admin_resellers and "
                        "app.services.reseller_portal multi-commit onboarding"
                    ),
                    new_owner="auth.reseller_onboarding",
                    verification=(
                        "Focused atomicity, delivery, reset, adapter, manifest, "
                        "and architecture-boundary tests."
                    ),
                    cutover_gate=(
                        "Admin reseller creation, add-user, and remove-access routes "
                        "call only typed owner commands."
                    ),
                    fallback_retirement=(
                        "Compensating deletion, direct onboarding commits, and "
                        "synchronous invite delivery are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_reseller_onboarding.py",
                    "tests/architecture/test_reseller_onboarding_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.oidc_mobile_federation",
            module="app.services.oidc_mobile_federation",
            owns=(
                "field mobile OIDC ceremony lifecycle",
                "field mobile OIDC assertion admission",
            ),
            depends_on=(
                "auth.session_staging",
                "control.feature_registry",
                "control.settings_spec",
                "events.dispatcher",
                "observability.audit_log",
                "party.credential_authentication_projection",
                "party.staff_authentication_reader",
            ),
            notes=(
                "Sub remains the session authority; the identity provider is "
                "a transport carrying one fact, that an issuer authenticated "
                "a subject. External roles, groups and authorization scopes "
                "have no effect: they are not read, stored or projected, so "
                "there is no path from a token claim to an access decision. "
                "This owner coordinates admission and session staging in one "
                "atomic transaction through auth.session_staging; tokens are "
                "encoded from the committed snapshot. It never "
                "provisions: an unbound subject is refused, there is no "
                "match-by-email and no just-in-time party creation."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="field mobile OIDC ceremony lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "field mobile federation enablement",
                            "field mobile federation configuration",
                            "outstanding field mobile ceremony record",
                        ),
                        canonical_writer="auth.oidc_mobile_federation",
                    ),
                    ConcernContract(
                        name="field mobile OIDC assertion admission",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "field mobile federation enablement",
                            "field mobile federation configuration",
                            "outstanding field mobile ceremony record",
                            "verified identity provider assertion",
                            "installed OIDC verifier binding",
                            "eligible staff principal",
                            "authentication session staging participant",
                        ),
                        canonical_writer="auth.oidc_mobile_federation",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="field mobile federation enablement",
                        owner="control.feature_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "the auth.oidc_mobile_federation control; default "
                            "off and fail closed, so both endpoints refuse "
                            "every request until an operator turns it on"
                        ),
                    ),
                    AuthorityInput(
                        name="field mobile federation configuration",
                        owner="control.settings_spec",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "auth-domain oidc_mobile_* settings; the issuer, "
                            "client id, redirect URI, audience, verifier "
                            "binding key and deployment id do not inherit "
                            "across scopes"
                        ),
                    ),
                    AuthorityInput(
                        name="outstanding field mobile ceremony record",
                        owner="auth.oidc_mobile_federation",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "oidc_mobile_ceremonies; a nonce hash and the "
                            "pinned bindings, never the nonce and never a "
                            "PKCE verifier"
                        ),
                    ),
                    AuthorityInput(
                        name="verified identity provider assertion",
                        owner="external:oidc_issuer",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "one RS256 ID token whose signature, issuer, "
                            "audience, authorized party, validity window, age "
                            "and nonce have all been verified against the "
                            "pinned issuer's JWKS"
                        ),
                    ),
                    AuthorityInput(
                        name="installed OIDC verifier binding",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the active authentication_bindings row for the "
                            "configured binding key and its one active "
                            "credential for the verified external subject"
                        ),
                    ),
                    AuthorityInput(
                        name="eligible staff principal",
                        owner="party.staff_authentication_reader",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Party-keyed staff principal resolution from the "
                            "credential's party_id, with system_user_id "
                            "compared as the Sub-owned context assertion"
                        ),
                    ),
                    AuthorityInput(
                        name="authentication session staging participant",
                        owner="auth.session_staging",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Typed flush-only session insertion, same-device supersession and staff presence, returning an immutable signing snapshot for post-commit encoding.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="Each endpoint enters execute_owner_command once on a transaction-free session. Successful exchange commits ceremony completion, credential bookkeeping, canonical AuditEvent, admitted outbox event, session, device supersession and staff presence together. Expected admission refusals return values so failed ceremony burns commit without a session. Storage or commit failure rolls back the success transition. Encoding occurs only after commit and cannot undo committed state.",
                    locking="Ceremony FOR UPDATE precedes immutable candidate discovery. Party-led principal resolution identifies the exact context, then fresh principal, credential, Party FK KEY SHARE and authentication-binding locks follow the reviewed identity dependency order. Exact version, provider, subject, projection and binding timestamp evidence are rechecked; drift is refused without selecting another identity. PostgreSQL lock_timeout is transaction-local five seconds per acquisition, not a total request deadline.",
                    idempotency="Single-use ceremony consumption serializes concurrent redemption. Expected denials commit a failed burn. A storage rollback leaves the ceremony retryable; a post-commit encoding failure leaves it consumed and requires a fresh ceremony. Same-device replacement participates in the session transaction.",
                    retries="Lock timeout returns a typed operational error after rollback. Retry requires a fresh request and complete revalidation; no nested session or audit retry. Authentication refusals and consumed ceremonies are not retried.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.oidc_mobile_federation.active_caller_transaction",
                        "auth.oidc_mobile_federation.command_contract_violation",
                        "auth.oidc_mobile_federation.configuration_incomplete",
                        "auth.oidc_mobile_federation.invalid_command_context",
                        "auth.oidc_mobile_federation.nested_owner_command",
                        "auth.oidc_mobile_federation.nested_transaction_completion",
                        "auth.oidc_mobile_federation.refused",
                        "auth.oidc_mobile_federation.lock_timeout",
                    ),
                    retryable_codes=("auth.oidc_mobile_federation.lock_timeout",),
                    mapping_owner="app.api.oidc_mobile",
                    fail_closed_on=(
                        "a disabled or unconfigured federation",
                        "an unknown, expired or already-used ceremony",
                        "any binding, nonce, signature or claim mismatch",
                        "a verified subject with no active local binding",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "oidc_mobile_ceremony.started",
                        "oidc_mobile_assertion.admitted",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries the ceremony, binding and "
                        "deployment identities and the admitted principal. It "
                        "carries no token, nonce, external subject or email, "
                        "and a later version may not add one."
                    ),
                    replay=(
                        "The ceremony row is the durable state and is single "
                        "use, so a redelivered event cannot readmit anything."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.oidc_mobile_federation",
                    verification=(
                        "Behaviour tests over the full refusal vocabulary and "
                        "an architecture boundary test."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/OIDC_MOBILE_FEDERATION.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_oidc_mobile_federation.py",
                    "tests/architecture/test_oidc_mobile_federation_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.api.*",
        "app.web.admin.*",
        "app.web.auth.*",
        "app.web.customer.auth",
    ),
    rule="Routes declare permission requirements; RBAC services own role and "
    "permission mutation. Business services should receive an authorized "
    "principal, not perform route-level permission wiring.",
)
