"""Canonical SOT declarations for the workforce_operations domain."""

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

DOMAIN = DomainSOT(
    domain="workforce_operations",
    services=(
        SOTService(
            name="operations.field_presence",
            module="app.services.field.presence",
            owns=("field workforce presence status",),
            depends_on=(
                "auth.permission_gate",
                "auth.staff_provisioning",
                "events.store",
            ),
            notes=(
                "Sub owns technician shift/presence vocabulary and state. The "
                "positioning owner neither accepts nor mutates workforce status; "
                "the legacy mixed row is split by canonical column writer until "
                "the dotmac-positioning cutover gives location its own table."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="field workforce presence status",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed field presence status command",
                            "authenticated field technician principal",
                            "staff-linked field technician profile",
                            "current field workforce presence status",
                        ),
                        canonical_writer="operations.field_presence",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed field presence status command",
                        owner="operations.field_presence",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "UpdateFieldPresenceStatusCommand with a Sub-owned "
                            "workforce status and command context"
                        ),
                    ),
                    AuthorityInput(
                        name="authenticated field technician principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="authenticated API principal passed as LocationPrincipal",
                    ),
                    AuthorityInput(
                        name="staff-linked field technician profile",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active TechnicianProfile bound to the authenticated "
                            "staff identity"
                        ),
                    ),
                    AuthorityInput(
                        name="current field workforce presence status",
                        owner="operations.field_presence",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "FieldTechPresence.status during the mixed-row migration; "
                            "no positioning caller writes that column"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each status mutation enters execute_owner_command once on a "
                        "transaction-free session and commits its event with the status."
                    ),
                    locking=(
                        "Technician identity and its unique presence row arbitrate one "
                        "current status per technician."
                    ),
                    idempotency=(
                        "Assigning the same canonical status is an equivalent state "
                        "transition and remains safe to retry."
                    ),
                    retries=(
                        "Retry the complete owner command only after rollback; invalid "
                        "status or technician identity requires caller correction."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "field_presence_invalid_status",
                        "field_presence_technician_not_found",
                        *owner_command_boundary_error_codes(
                            "operations.field_presence"
                        ),
                    ),
                    mapping_owner="field location API adapter",
                    fail_closed_on=(
                        "unknown workforce status",
                        "missing or inactive technician identity",
                    ),
                ),
                events=EventContract(
                    event_types=("field_presence.changed",),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility=(
                        "Version 1 carries the Sub technician identity and canonical "
                        "workforce status; it carries no coordinates or collection grant."
                    ),
                    replay=(
                        "Current FieldTechPresence.status plus the durable event log "
                        "proves the latest applied product status."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner=(
                        "field location ingestion and collection-grant helpers that "
                        "also mutated workforce status"
                    ),
                    new_owner="operations.field_presence",
                    verification=(
                        "architecture, invalid-status, collection separation, and "
                        "mobile payload tests"
                    ),
                    cutover_gate=(
                        "PositionObservation and UpdateLocationCollectionCommand carry "
                        "no status, and only app.services.field.presence writes it."
                    ),
                    fallback_retirement=(
                        "Status in position ping payloads and positioning-owner status "
                        "normalization are removed."
                    ),
                ),
                steward="field operations",
                design_refs=(
                    "docs/designs/POSITION_OBSERVATION_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_field_location_tracking.py",
                    "tests/architecture/test_position_observation_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="operations.field_geofence_policy",
            module="app.services.field.geofence",
            owns=("field work-order geofence consequence policy",),
            depends_on=(
                "operations.position_observations",
                "operations.field_completion",
                "control.domain_settings",
                "events.store",
            ),
            notes=(
                "A durable opaque position-observation event is the trigger. This "
                "product policy loads retained evidence, accepts only the current "
                "fresh projection, and asks operations.field_completion to perform "
                "the deterministic transition. Positioning never owns or executes "
                "the work-order consequence."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="field work-order geofence consequence policy",
                        role=OwnerRole.EVENT_POLICY,
                        input_names=(
                            "committed field position observation event",
                            "retained field position observation",
                            "current field technician position projection",
                            "field geofence policy settings",
                            "current field work-order transition state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="committed field position observation event",
                        owner="events.store",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "durable position_observation.recorded event carrying "
                            "only the opaque observation identity"
                        ),
                    ),
                    AuthorityInput(
                        name="retained field position observation",
                        owner="operations.position_observations",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "immutable FieldTechLocationPing reloaded by the event's "
                            "opaque observation identity"
                        ),
                    ),
                    AuthorityInput(
                        name="current field technician position projection",
                        owner="operations.position_observations",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "current-position fields canonically written from the "
                            "newest credible retained observation"
                        ),
                    ),
                    AuthorityInput(
                        name="field geofence policy settings",
                        owner="control.domain_settings",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "database-authoritative enablement, arrival-radius, and "
                            "observation-freshness settings"
                        ),
                    ),
                    AuthorityInput(
                        name="current field work-order transition state",
                        owner="operations.field_completion",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "assigned scheduled/dispatched WorkOrder state plus the "
                            "canonical field transition engine"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "The policy writes no state. The durable event dispatcher owns "
                        "the isolated handler transaction and operations.field_completion "
                        "owns every accepted work-order transition."
                    ),
                    locking=(
                        "The policy reads retained observation and current projection; "
                        "the field-transition owner applies its own transition guards."
                    ),
                    idempotency=(
                        "A stable UUIDv5 client-event identity per work-order start makes "
                        "redelivery an exact field-transition replay."
                    ),
                    retries=(
                        "Retry the durable handler with the same event identity; stale, "
                        "superseded, disabled, or ineligible evidence remains a no-op."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="durable field geofence event handler",
                    fail_closed_on=(
                        "missing retained observation or current projection",
                        "stale or superseded evidence",
                        "disabled policy or ineligible work-order state",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner=(
                        "best-effort geofence evaluation inside location ingestion "
                        "after an independently committed observation"
                    ),
                    new_owner="operations.field_geofence_policy",
                    verification=(
                        "durable event, stale evidence, projection currency, exact "
                        "redelivery, and consequence-separation tests"
                    ),
                    cutover_gate=(
                        "Position ingestion emits only opaque evidence and never imports "
                        "or invokes the field transition engine."
                    ),
                    fallback_retirement=(
                        "Direct geofence invocation and post-commit mutation in the "
                        "positioning writer are removed."
                    ),
                ),
                steward="field operations",
                design_refs=(
                    "docs/designs/POSITION_OBSERVATION_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_field_location_tracking.py",
                    "tests/architecture/test_position_observation_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="operations.position_observations",
            module="app.services.field.location_tracking",
            owns=(
                "field position observations",
                "field technician current-position projection",
                "field location collection grant",
                "position observation retention",
            ),
            depends_on=(
                "auth.permission_gate",
                "auth.staff_provisioning",
                "events.store",
            ),
            notes=(
                "This product-first source owns provider-neutral location evidence, "
                "replay identity, and the rebuildable current-position snapshot. It "
                "does not own work-order, dispatch, attendance, customer-disclosure, "
                "or other business consequences. Starter ADR-0032 governs extraction "
                "into dotmac-positioning."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="field position observations",
                        role=OwnerRole.OBSERVATION_COLLECTOR,
                        input_names=(
                            "typed field position observation batch",
                            "authenticated field technician principal",
                            "staff-linked field technician profile",
                        ),
                        canonical_writer="operations.position_observations",
                    ),
                    ConcernContract(
                        name="field technician current-position projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=("current field position observations",),
                        canonical_writer="operations.position_observations",
                    ),
                    ConcernContract(
                        name="field location collection grant",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed field location collection command",
                            "authenticated field technician principal",
                            "staff-linked field technician profile",
                        ),
                        canonical_writer="operations.position_observations",
                    ),
                    ConcernContract(
                        name="position observation retention",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "typed position observation retention command",
                            "current field position observations",
                        ),
                        canonical_writer="operations.position_observations",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed field position observation batch",
                        owner="operations.position_observations",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "RecordLocationBatchCommand with immutable client identity, "
                            "device capture time, coordinates, accuracy, source, and "
                            "optional opaque product context plus typed product policy"
                        ),
                    ),
                    AuthorityInput(
                        name="typed field location collection command",
                        owner="operations.position_observations",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "UpdateLocationCollectionCommand with explicit enabled state, "
                            "purpose, and bounded expiry"
                        ),
                    ),
                    AuthorityInput(
                        name="typed position observation retention command",
                        owner="operations.position_observations",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "PrunePositionObservationsCommand with the resolved "
                            "database-authoritative retention window"
                        ),
                    ),
                    AuthorityInput(
                        name="authenticated field technician principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="authenticated API principal passed as LocationPrincipal",
                    ),
                    AuthorityInput(
                        name="staff-linked field technician profile",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active TechnicianProfile bound to the authenticated staff identity",
                    ),
                    AuthorityInput(
                        name="current field position observations",
                        owner="operations.position_observations",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "immutable FieldTechLocationPing rows accepted under one "
                            "technician, source, and client-observation identity"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each ingest or collection-grant mutation enters "
                        "execute_owner_command once on a transaction-free session; "
                        "observation, projection, and grant state commit together."
                    ),
                    locking=(
                        "The technician profile and presence identity are resolved before "
                        "writes; the technician/source/client-observation unique key "
                        "arbitrates concurrent evidence."
                    ),
                    idempotency=(
                        "An exact observation-identity and payload-fingerprint replay is a "
                        "typed no-op; changed evidence under that identity fails closed."
                    ),
                    retries=(
                        "Retry the complete owner command only after rollback; an identity "
                        "collision requires caller adjudication, never payload replacement."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "position_observation_invalid_principal",
                        "position_observation_technician_not_found",
                        "position_observation_invalid_source",
                        "position_observation_invalid_coordinates",
                        "position_observation_invalid_accuracy",
                        "position_observation_future_timestamp",
                        "position_observation_invalid_context",
                        "position_observation_invalid_collection_purpose",
                        "position_observation_invalid_policy",
                        "position_observation_invalid_collection_grant",
                        "position_observation_collection_not_granted",
                        "position_observation_invalid_retention",
                        "position_observation_identity_collision",
                        "position_observation_empty_batch",
                        "position_observation_batch_too_large",
                        *owner_command_boundary_error_codes(
                            "operations.position_observations"
                        ),
                    ),
                    mapping_owner="field location API adapter",
                    fail_closed_on=(
                        "missing or inactive technician identity",
                        "missing, expired, or malformed field-operations collection grant",
                        "invalid coordinates, accuracy, source, policy, or capture time",
                        "changed evidence under an existing observation identity",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "position_observation.recorded",
                        "position_observation.pruned",
                        "position_collection.changed",
                    ),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility=(
                        "Version 1 carries opaque observation and technician identities or "
                        "collection state; it never duplicates coordinates in the outbox."
                    ),
                    replay=(
                        "Observation identity and payload fingerprint prove evidence replay; "
                        "collection state is reconstructed from FieldTechPresence."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="field technician current-position projection",
                        input_names=("current field position observations",),
                        writer="operations.position_observations",
                        freshness=(
                            "Transaction-current newest credible device capture time; an "
                            "equal-time fix advances only when it has better accuracy."
                        ),
                        stale_behavior=(
                            "Keep the last credible projection and let each authorized "
                            "reader apply its own server-side freshness cutoff."
                        ),
                        drift_signal=(
                            "Presence coordinates or capture time differ from deterministic "
                            "selection over retained observations."
                        ),
                        rebuild_operation=(
                            "Select the freshest credible retained observation per technician, "
                            "breaking equal capture time by best accuracy."
                        ),
                        repair_owner="operations.position_observations",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner=(
                        "uncontracted field location helpers with nested commits and "
                        "best-effort post-commit geofence mutation"
                    ),
                    new_owner="operations.position_observations",
                    verification=(
                        "rejected-row, exact-replay, identity-collision, canonical work-order, "
                        "capture-time, accuracy, transaction-boundary, and architecture tests"
                    ),
                    cutover_gate=(
                        "The API passes typed commands to the registered owner and no location "
                        "helper commits, rolls back, or executes product consequences."
                    ),
                    fallback_retirement=(
                        "Legacy record_ping, dict batch payloads, crm_work_order_id API output, "
                        "and post-commit geofence evaluation are removed."
                    ),
                ),
                steward="field operations",
                design_refs=(
                    "docs/designs/POSITION_OBSERVATION_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_field_location_tracking.py",
                    "tests/architecture/test_position_observation_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="operations.service_team_source_retirement",
            module="app.services.service_team_source_retirement",
            owns=(
                "legacy service-team source retirement",
                "legacy service-team source-retirement readiness",
            ),
            depends_on=(
                "operations.service_team_lifecycle",
                "events.store",
                "observability.audit_log",
            ),
            notes=(
                "The one-time pre-426 owner verifies retained native "
                "team pointers, retires workflow-setting sources, clears the "
                "non-authoritative manager pointer, and removes only membership "
                "rows that migration 426 would reject. It never reads CRM, "
                "chooses a conflicting identity, or creates identities, "
                "memberships, or grants."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="legacy service-team source retirement",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "native service-team identity pointers",
                            "legacy workflow service-team sources",
                        ),
                        canonical_writer=("operations.service_team_source_retirement"),
                    ),
                    ConcernContract(
                        name="legacy service-team source-retirement readiness",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "native service-team identity pointers",
                            "legacy workflow service-team sources",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="native service-team identity pointers",
                        owner="operations.service_team_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "retained ticket, project, dispatch, Inbox-route, "
                            "AI-route, channel-route, and Inbox-conversation "
                            "team pointers"
                        ),
                    ),
                    AuthorityInput(
                        name="legacy workflow service-team sources",
                        owner="operations.service_team_source_retirement",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "active support_service_teams and "
                            "support_service_team_members settings plus legacy "
                            "manager and migration-blocking membership pointers"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "The explicit operator command enters "
                        "execute_owner_command once; source, pointer, audit, "
                        "and event changes flush in the root transaction."
                    ),
                    locking=(
                        "Teams, memberships, and legacy settings lock in stable "
                        "identifier order after the five pointer checks."
                    ),
                    idempotency=(
                        "A fully retired source replays with zero mutations; "
                        "constraints and the pointer audit reject changed state."
                    ),
                    retries=(
                        "Retry the whole command only after rollback; dangling "
                        "pointers and source mismatches require adjudication."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "service_team_source_retirement_dangling_pointer",
                        "service_team_source_retirement_duplicate_name",
                        "service_team_source_retirement_malformed",
                        "service_team_source_retirement_team_mismatch",
                        *owner_command_boundary_error_codes(
                            "operations.service_team_source_retirement"
                        ),
                    ),
                    mapping_owner=(
                        "scripts.migration.retire_legacy_service_team_sources"
                    ),
                    fail_closed_on=(
                        "any dangling native team pointer",
                        "duplicate native name or source/native identity mismatch",
                        "malformed active workflow source",
                    ),
                ),
                events=EventContract(
                    event_types=("service_team.changed",),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility=(
                        "Version 1 records aggregate pointer and retirement "
                        "counts without staff or CRM identity."
                    ),
                    replay=(
                        "The audit plus inactive sources, null manager pointers, "
                        "and resolvable remaining memberships prove retirement."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner="legacy workflow settings and imported scalar pointers",
                    new_owner="operations.service_team_source_retirement",
                    verification=(
                        "five-pointer, malformed-source, mismatch, exact replay, "
                        "migration-426, and architecture tests"
                    ),
                    cutover_gate=(
                        "The read-only deploy check reports five pointer "
                        "contracts, zero dangling references, and zero active "
                        "legacy sources or migration-426 identity blockers."
                    ),
                    fallback_retirement=(
                        "No CRM membership planner, identity review, adoption "
                        "coordinator, or email-matching fallback remains."
                    ),
                ),
                steward="operations administration",
                design_refs=(
                    "docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md",
                    "docs/runbooks/SERVICE_TEAM_PARTY_CUTOVER.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_service_team_source_retirement.py",
                    ("tests/architecture/test_service_team_lifecycle_boundary.py"),
                ),
            ),
        ),
        SOTService(
            name="operations.service_team_lifecycle",
            module="app.services.service_team_lifecycle",
            owns=(
                "service-team lifecycle",
                "service-team membership lifecycle",
                "set-valued staff service-team membership resolution",
                "active service-team selector projection",
                "service-team administration projection",
            ),
            depends_on=(
                "party.registry",
                "auth.staff_provisioning",
                "events.store",
                "observability.audit_log",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="service-team lifecycle",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed service-team command",
                            "active staff authentication principal",
                            "canonical Person Party identity",
                            "current native service-team state",
                        ),
                        canonical_writer="operations.service_team_lifecycle",
                    ),
                    ConcernContract(
                        name="service-team membership lifecycle",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed service-team command",
                            "active staff authentication principal",
                            "canonical Person Party identity",
                            "current native service-team state",
                        ),
                        canonical_writer="operations.service_team_lifecycle",
                    ),
                    ConcernContract(
                        name="set-valued staff service-team membership resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "active staff authentication principal",
                            "canonical Person Party identity",
                            "current native service-team state",
                        ),
                    ),
                    ConcernContract(
                        name="active service-team selector projection",
                        role=OwnerRole.RESOLVER,
                        input_names=("current native service-team state",),
                    ),
                    ConcernContract(
                        name="service-team administration projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "active staff authentication principal",
                            "canonical Person Party identity",
                            "current native service-team state",
                            "current service-team composition",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed service-team command",
                        owner="operations.service_team_lifecycle",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed identity, activation, and membership "
                            "commands with CommandContext and expected state"
                        ),
                    ),
                    AuthorityInput(
                        name="active staff authentication principal",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active SystemUser staff login and authorization principal"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Person Party and reviewed "
                            "SystemUser.person_party_id identity binding"
                        ),
                    ),
                    AuthorityInput(
                        name="current native service-team state",
                        owner="operations.service_team_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="ServiceTeam and ServiceTeamMember rows",
                    ),
                    AuthorityInput(
                        name="current service-team composition",
                        owner="operations.service_team_composition",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "composition-owned set-valued capability, "
                            "responsibility, and typed scope queries"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public mutation enters execute_owner_command once on a "
                        "transaction-free session; audit, outbox, team, and membership changes "
                        "flush inside that root transaction."
                    ),
                    locking=(
                        "Teams are selected by UUID, followed by staff-principal and Person "
                        "Party identity then membership locks; case-insensitive team-name "
                        "and team/person constraints arbitrate concurrent writes."
                    ),
                    idempotency=(
                        "Create binds a caller-supplied team UUID; equivalent desired-state "
                        "updates, activation changes, and membership commands replay while a "
                        "deactivated row or changed evidence under one create identity fails "
                        "closed."
                    ),
                    retries=(
                        "Adapters retry the complete owner command only after full rollback "
                        "and refetch current updated_at evidence after a stale rejection."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "service_team_invalid",
                        "service_team_not_found",
                        "service_team_staff_not_found",
                        "service_team_staff_identity_unbound",
                        "service_team_staff_identity_invalid",
                        "service_team_name_conflict",
                        "service_team_identity_collision",
                        "service_team_stale",
                        "service_team_reason_required",
                        "service_team_inactive",
                        "service_team_has_active_members",
                        "service_team_member_not_found",
                        "service_team_member_inactive",
                        *owner_command_boundary_error_codes(
                            "operations.service_team_lifecycle"
                        ),
                    ),
                    mapping_owner="service-team web and API adapters",
                    fail_closed_on=(
                        "unknown, inactive, or Party-unbound selected staff identity",
                        "membership reactivation with retired staff identity",
                        "stale lifecycle evidence",
                        "team identity collision",
                        "deactivation with active members",
                        "unavailable Party identity during set-valued resolution",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "service_team.changed",
                        "service_team.membership_changed",
                    ),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility=(
                        "Version 1 carries team, command/correlation, operation, "
                        "member, and actor identifiers without private staff payloads."
                    ),
                    replay=(
                        "Native team/member rows plus transactional event and audit evidence "
                        "reconstruct the lifecycle without workflow-setting mirrors."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="set-valued staff service-team membership resolution",
                        input_names=(
                            "active staff authentication principal",
                            "canonical Person Party identity",
                            "current native service-team state",
                            "current service-team composition",
                        ),
                        writer="operations.service_team_lifecycle",
                        freshness="Transaction-current native database state.",
                        stale_behavior=(
                            "Return identity-unavailable, no-membership, or every "
                            "active membership; never select by row age."
                        ),
                        drift_signal=(
                            "A caller requiring one team consumes membership "
                            "instead of an explicit assignment or routing policy."
                        ),
                        rebuild_operation=(
                            "Requery the reviewed SystemUser-to-Party binding and native "
                            "active memberships."
                        ),
                        repair_owner="operations.service_team_lifecycle",
                    ),
                    ProjectionContract(
                        name="active service-team selector projection",
                        input_names=("current native service-team state",),
                        writer="operations.service_team_lifecycle",
                        freshness="Transaction-current native database state.",
                        stale_behavior=(
                            "Fail the request rather than use workflow-setting fallback."
                        ),
                        drift_signal=(
                            "Legacy workflow-setting team/member keys exist or differ from "
                            "native row identity and active membership."
                        ),
                        rebuild_operation=(
                            "Requery native ServiceTeam and ServiceTeamMember rows."
                        ),
                        repair_owner="operations.service_team_lifecycle",
                    ),
                    ProjectionContract(
                        name="service-team administration projection",
                        input_names=(
                            "active staff authentication principal",
                            "canonical Person Party identity",
                            "current native service-team state",
                        ),
                        writer="operations.service_team_lifecycle",
                        freshness="Transaction-current native database state.",
                        stale_behavior=(
                            "Render an explicit error; never fall back to retired settings."
                        ),
                        drift_signal=(
                            "Membership references an absent staff principal, "
                            "a duplicate case-insensitive team name exists, or "
                            "the composition owner reports governed-key drift."
                        ),
                        rebuild_operation=(
                            "Recompose identity and membership locally, then "
                            "join composition-owner capability, responsibility, "
                            "and typed-scope sets."
                        ),
                        repair_owner="operations.service_team_lifecycle",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner=(
                        "support.ticket_configuration workflow-setting team/member payloads "
                        "and their settings-to-native mirror"
                    ),
                    new_owner="operations.service_team_lifecycle",
                    verification=(
                        "service-team owner behavior, migration, admin-surface, caller, and "
                        "architecture tests"
                    ),
                    cutover_gate=(
                        "settings payloads are backfilled and verified; every caller reads "
                        "native projections; only this owner writes team/member rows"
                    ),
                    fallback_retirement=(
                        "support_service_teams/support_service_team_members keys, ticket-"
                        "settings editors, mirror helper, CRM hard-delete path, and direct "
                        "team/member writers, including the old provider direct writer and "
                        "email-matching agent projection, are absent"
                    ),
                ),
                steward="operations administration",
                design_refs=(
                    "docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_service_team_lifecycle.py",
                    "tests/test_service_team_web.py",
                    "tests/architecture/test_service_team_lifecycle_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="operations.service_team_composition",
            module="app.services.service_team_composition",
            owns=(
                "service-team composition lifecycle",
                "explicit service-team routing policy",
                (
                    "set-valued service-team capability, responsibility, "
                    "and scope resolution"
                ),
                "service-team composition shadow verification",
            ),
            depends_on=(
                "operations.service_team_lifecycle",
                "party.registry",
                "auth.staff_provisioning",
                "gis.spatial_sync",
                "events.store",
                "observability.audit_log",
            ),
            notes=(
                "Stable team identity composes governed capabilities, "
                "Party-backed membership responsibilities, typed GeoArea or "
                "global scope, explicit topology, provider-neutral external "
                "observations, and domain route keys. Responsibilities never "
                "grant RBAC; consumers intersect authorized access with the "
                "returned operational scope. Code-consumed domain route keys "
                "are registered with a domain owner, version, and required "
                "capability. Geo-scoped route resolution accepts one "
                "caller-derived effective GeoArea and never derives it from "
                "topology itself: the network-zone catalog "
                "(app.services.network.zones, a legacy-baseline writer) is "
                "the single writer of the zone -> GeoArea binding, and "
                "consumers such as outage routing derive an incident's "
                "effective GeoArea only through NetworkZones.resolve_geo_area "
                "(parent-chain inheritance). Intentionally unbound zones "
                "may use configured global routing; a stale binding — a "
                "retired GeoArea on the nearest bound zone — resolves "
                "unavailable and denies the scoped routing consequence, "
                "never masquerading as unbound or rebinding wider."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="service-team composition lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "typed service-team composition command",
                            "native service-team identity and membership",
                            "registered service-team capability vocabulary",
                            "typed geographic scope record",
                        ),
                        canonical_writer="operations.service_team_composition",
                    ),
                    ConcernContract(
                        name="explicit service-team routing policy",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "typed service-team routing decision",
                            "registered service-team routing vocabulary",
                            "native service-team identity and membership",
                            "registered service-team capability vocabulary",
                            "typed geographic scope record",
                        ),
                        canonical_writer="operations.service_team_composition",
                    ),
                    ConcernContract(
                        name=(
                            "set-valued service-team capability, responsibility, "
                            "and scope resolution"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "native service-team identity and membership",
                            "registered service-team capability vocabulary",
                            "typed geographic scope record",
                        ),
                    ),
                    ConcernContract(
                        name="service-team composition shadow verification",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "native service-team identity and membership",
                            "legacy service-team scalar shadow",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed service-team composition command",
                        owner="operations.service_team_composition",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed capability, responsibility, GeoArea scope, "
                            "relationship, and external-observation commands"
                        ),
                    ),
                    AuthorityInput(
                        name="typed service-team routing decision",
                        owner="operations.service_team_composition",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "domain, route key, exact team, optional typed "
                            "scope, priority, and active state"
                        ),
                    ),
                    AuthorityInput(
                        name="registered service-team routing vocabulary",
                        owner="operations.service_team_composition",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "versioned domain/route contracts naming the "
                            "domain owner and required governed capability"
                        ),
                    ),
                    AuthorityInput(
                        name="native service-team identity and membership",
                        owner="operations.service_team_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active ServiceTeam identity and Party-backed "
                            "ServiceTeamMember membership"
                        ),
                    ),
                    AuthorityInput(
                        name="registered service-team capability vocabulary",
                        owner="operations.service_team_composition",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "versioned capability definition rows matching "
                            "the code registry; no team or access seed data"
                        ),
                    ),
                    AuthorityInput(
                        name="typed geographic scope record",
                        owner="gis.spatial_sync",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active native GeoArea identity",
                    ),
                    AuthorityInput(
                        name="legacy service-team scalar shadow",
                        owner="operations.service_team_source_retirement",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "nullable team_type, region, manager_person_id, "
                            "membership role, and workforce reference retained "
                            "only for migration comparison"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public composition mutation enters "
                        "execute_owner_command once; the binding, audit, and "
                        "event flush in the same root transaction."
                    ),
                    locking=(
                        "Team then membership/definition/scope/policy records "
                        "lock in stable identifier order; unique constraints "
                        "arbitrate concurrent assignment."
                    ),
                    idempotency=(
                        "Equivalent active-state commands replay. Routing and "
                        "provider-reference identifiers reject changed evidence."
                    ),
                    retries=(
                        "Retry the entire owner command after rollback; stale "
                        "identity, inactive scope, and ambiguity are not "
                        "automatically repairable."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "service_team_composition_invalid",
                        "service_team_capability_unregistered",
                        "service_team_capability_contract_drift",
                        "service_team_responsibility_contract_drift",
                        "service_team_scope_invalid",
                        "service_team_scope_contract_drift",
                        "service_team_scope_geo_area_inactive",
                        "service_team_geo_area_not_found",
                        "service_team_scope_not_found",
                        "service_team_relationship_invalid",
                        "service_team_relationship_cycle",
                        "service_team_external_reference_conflict",
                        "service_team_routing_invalid",
                        "service_team_routing_unregistered",
                        "service_team_routing_capability_missing",
                        "service_team_routing_identity_collision",
                        "service_team_routing_ambiguous",
                        "service_team_not_found",
                        "service_team_member_not_found",
                        "service_team_member_inactive",
                        "service_team_inactive",
                        *owner_command_boundary_error_codes(
                            "operations.service_team_composition"
                        ),
                    ),
                    mapping_owner="domain route and administration adapters",
                    fail_closed_on=(
                        "unregistered capability or inactive team/member/scope",
                        "unregistered domain route or capability-ineligible team",
                        "provider-reference identity reuse",
                        "multiple winning explicit routing policies",
                        "missing required explicit route",
                    ),
                ),
                events=EventContract(
                    event_types=("service_team.changed",),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility=(
                        "Version 1 carries governed keys, team/membership/"
                        "scope/policy identifiers, and active state without "
                        "private staff or provider payloads."
                    ),
                    replay=(
                        "Native definition and binding rows plus event/audit "
                        "evidence reconstruct composition."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name=(
                            "set-valued service-team capability, responsibility, "
                            "and scope resolution"
                        ),
                        input_names=(
                            "native service-team identity and membership",
                            "registered service-team capability vocabulary",
                            "typed geographic scope record",
                        ),
                        writer="operations.service_team_composition",
                        freshness="Transaction-current native database state.",
                        stale_behavior=(
                            "Return an empty set for unavailable identity or "
                            "missing scope; never choose a row by age."
                        ),
                        drift_signal=(
                            "A consumer reads scalar team type, region, "
                            "manager, role, or workforce columns."
                        ),
                        rebuild_operation=(
                            "Requery active memberships and active governed "
                            "capability, responsibility, and scope bindings."
                        ),
                        repair_owner="operations.service_team_composition",
                    ),
                    ProjectionContract(
                        name="service-team composition shadow verification",
                        input_names=(
                            "native service-team identity and membership",
                            "legacy service-team scalar shadow",
                        ),
                        writer="operations.service_team_composition",
                        freshness="On-demand transaction-current comparison.",
                        stale_behavior=(
                            "Any unmatched shadow pointer blocks contract."
                        ),
                        drift_signal=(
                            "One of the five scalar shadow comparisons is nonzero."
                        ),
                        rebuild_operation=(
                            "Idempotently rerun the forward backfill and bind "
                            "legacy region labels only through reviewed GeoAreas."
                        ),
                        repair_owner="operations.service_team_composition",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "team_type, region, manager_person_id, membership role, "
                        "and workforce department scalar columns"
                    ),
                    new_owner="operations.service_team_composition",
                    verification=(
                        "migration 440, owner commands, set-valued queries, "
                        "explicit routing, five-field shadow drift, and "
                        "architecture consumer guards"
                    ),
                    cutover_gate=(
                        "All consumers use composition, a complete shadow run "
                        "has zero drift, every legacy region is reviewed against "
                        "GeoArea, and rollback requirements have expired."
                    ),
                    fallback_retirement=(
                        "Only then may a forward contract migration drop "
                        "team_type, region, manager_person_id, membership role, "
                        "workforce_system, and workforce_department_reference."
                    ),
                ),
                steward="operations administration",
                design_refs=(
                    "docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_service_team_composition.py",
                    "tests/test_team_outbound.py",
                    "tests/test_field_job_chat.py",
                    "tests/services/topology/test_outage_operations.py",
                    "tests/test_api_network_catalog.py",
                    "tests/test_workqueue_parity.py",
                    ("tests/architecture/test_service_team_lifecycle_boundary.py"),
                ),
            ),
        ),
        SOTService(
            name="operations.agent_workqueue",
            module="app.services.workqueue.commands",
            owns=(
                "agent workqueue scope and audience resolution",
                "agent workqueue prioritization projection",
                "personal workqueue snooze state",
                "agent workqueue action coordination",
            ),
            depends_on=(
                "auth.staff_provisioning",
                "operations.service_team_lifecycle",
                "operations.service_team_composition",
                "support.ticket_lifecycle",
                "support.ticket_sla_clock",
                "communications.team_inbox_projection",
                "communications.team_inbox_commands",
                "operations.work_orders",
                "events.store",
                "observability.audit_log",
            ),
            notes=(
                "The workqueue owns scope, ranking, and each operator's snooze "
                "state. Claim and complete are atomic coordinator commands: "
                "Ticket and Team Inbox owners retain every underlying lifecycle "
                "decision, while Work Orders remain open/snooze-only until their "
                "native dispatch owner exposes an approved inline transition."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="agent workqueue scope and audience resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "authenticated staff principal",
                            "native service-team scope",
                        ),
                    ),
                    ConcernContract(
                        name="agent workqueue prioritization projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "native service-team scope",
                            "canonical support-ticket state",
                            "canonical ticket SLA clocks",
                            "canonical Team Inbox projection",
                            "native work-order projection",
                            "personal workqueue snooze state",
                            "workqueue scoring policy",
                        ),
                    ),
                    ConcernContract(
                        name="personal workqueue snooze state",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authenticated staff principal",
                            "scope-checked workqueue action",
                            "personal workqueue snooze state",
                        ),
                        canonical_writer="operations.agent_workqueue",
                    ),
                    ConcernContract(
                        name="agent workqueue action coordination",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authenticated staff principal",
                            "native service-team scope",
                            "scope-checked workqueue action",
                            "canonical support-ticket state",
                            "canonical Team Inbox projection",
                            "workqueue action idempotency evidence",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated staff principal",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active authenticated SystemUser ID, roles, scopes, and "
                            "support ticket read/update authorization"
                        ),
                    ),
                    AuthorityInput(
                        name="native service-team scope",
                        owner="operations.service_team_composition",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Party-backed active membership plus queue-lead and "
                            "accountable-manager responsibility sets, intersected "
                            "with independently authorized RBAC audience"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical support-ticket state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Ticket identity, status, priority, assignment, "
                            "service team, due time, and lifecycle command outcome"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical ticket SLA clocks",
                        owner="support.ticket_sla_clock",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="running and breached ticket SlaClock rows",
                    ),
                    AuthorityInput(
                        name="canonical Team Inbox projection",
                        owner="communications.team_inbox_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "active conversation, owner team, assignment, latest inbound "
                            "message, status, priority, and lifecycle command outcome"
                        ),
                    ),
                    AuthorityInput(
                        name="native work-order projection",
                        owner="operations.work_orders",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "non-terminal native WorkOrder and dispatch assignment "
                            "projection; CRM compatibility IDs carry no action authority"
                        ),
                    ),
                    AuthorityInput(
                        name="personal workqueue snooze state",
                        owner="operations.agent_workqueue",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "WorkqueueSnooze rows keyed by authenticated SystemUser, "
                            "native item kind, and native item ID"
                        ),
                    ),
                    AuthorityInput(
                        name="workqueue scoring policy",
                        owner="operations.agent_workqueue",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed SLA bands, source scores, stable kind order, "
                            "provider limit, and right-now band configuration"
                        ),
                    ),
                    AuthorityInput(
                        name="scope-checked workqueue action",
                        owner="operations.agent_workqueue",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed item kind, native item ID, action, audience, team "
                            "filter, snooze mode, CommandContext, current action hints, "
                            "owner-generated state fingerprint, and explicit completion "
                            "confirmation"
                        ),
                    ),
                    AuthorityInput(
                        name="workqueue action idempotency evidence",
                        owner="operations.agent_workqueue",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked IdempotencyKey bound to actor, item kind, item ID, "
                            "and action"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "execute_action enters execute_owner_command once on a clean "
                        "session; scope, idempotency, target locks, source-owner "
                        "participants, snooze state, audit, and outbox evidence commit "
                        "as one root transaction."
                    ),
                    locking=(
                        "The idempotency row and native target record are selected FOR "
                        "UPDATE before current scope and action eligibility are checked; "
                        "claim/complete fingerprints and completion confirmation are "
                        "rechecked under that lock; source owners apply their own locked "
                        "lifecycle policy as flush-only participants."
                    ),
                    idempotency=(
                        "A mandatory caller key binds actor, item kind, native item ID, "
                        "and action; an exact replay returns its stored result without "
                        "reapplying the source transition."
                    ),
                    retries=(
                        "Adapters retry only after complete rollback with the same "
                        "idempotency key; stale scope, missing membership, and unavailable "
                        "actions fail closed."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "operations.agent_workqueue.item_not_found",
                        "operations.agent_workqueue.item_out_of_scope",
                        "operations.agent_workqueue.action_unavailable",
                        "operations.agent_workqueue.permission_denied",
                        "operations.agent_workqueue.team_required",
                        "operations.agent_workqueue.claim_rejected",
                        "operations.agent_workqueue.completion_rejected",
                        "operations.agent_workqueue.action_review_required",
                        "operations.agent_workqueue.stale_action_review",
                        "operations.agent_workqueue.confirmation_required",
                        "operations.agent_workqueue.idempotency_key_required",
                        "operations.agent_workqueue.invalid_idempotency_key",
                        "operations.agent_workqueue.idempotency_conflict",
                        "operations.agent_workqueue.invalid_item_kind",
                        "operations.agent_workqueue.invalid_snooze_mode",
                        *owner_command_boundary_error_codes(
                            "operations.agent_workqueue"
                        ),
                    ),
                    mapping_owner="workqueue API and admin web adapters",
                    fail_closed_on=(
                        "inactive or Party-unbound authenticated staff identity",
                        "requested audience or team outside native service-team scope",
                        "stale item action hints",
                        "missing or stale lifecycle-action review fingerprint",
                        "completion without explicit impact confirmation",
                        "target or idempotency mismatch",
                        "claim without active target-team membership",
                    ),
                ),
                events=EventContract(
                    event_types=("workqueue.action_coordinated",),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility=(
                        "Version 1 carries command/correlation, item kind and ID, "
                        "action, result, team, and assignee identifiers without "
                        "message or customer payloads."
                    ),
                    replay=(
                        "Canonical source rows, WorkqueueSnooze, IdempotencyKey, "
                        "transactional audit, and outbox evidence reconstruct actions."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="agent workqueue prioritization projection",
                        input_names=(
                            "native service-team scope",
                            "canonical support-ticket state",
                            "canonical ticket SLA clocks",
                            "canonical Team Inbox projection",
                            "native work-order projection",
                            "personal workqueue snooze state",
                            "workqueue scoring policy",
                        ),
                        writer="operations.agent_workqueue",
                        freshness=(
                            "Transaction-current reads plus best-effort realtime "
                            "invalidation and a 30-second browser repair poll."
                        ),
                        stale_behavior=(
                            "Render the last request result with its generated-at "
                            "timestamp; a realtime transport failure never changes facts "
                            "and the next poll rebuilds from authoritative inputs."
                        ),
                        drift_signal=(
                            "Provider parity tests or production comparison show an "
                            "eligible source item missing, mis-scoped, or ranked with "
                            "different authoritative inputs."
                        ),
                        rebuild_operation=(
                            "Resolve native service-team scope, fetch every registered "
                            "provider, apply personal snoozes, then deterministically "
                            "score and sort with stable kind and native-ID tie-breakers."
                        ),
                        repair_owner="operations.agent_workqueue",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner="dotmac_crm app/web/agent/workqueue.py",
                    new_owner="operations.agent_workqueue",
                    verification=(
                        "provider parity, scope, owner command, admin web, route, "
                        "ledger, and architecture tests"
                    ),
                    cutover_gate=(
                        "native operator surface and all seven route behaviors are "
                        "verified; production snooze data and callers are reconciled; "
                        "CRM route traffic is directed to Sub"
                    ),
                    fallback_retirement=(
                        "CRM workqueue routes, templates, action dispatcher, and "
                        "personal snooze writer are deleted after the defined zero-"
                        "traffic observation window"
                    ),
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/AGENT_WORKQUEUE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/CRM_WEB_RETIREMENT.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_workqueue_parity.py",
                    "tests/test_workqueue_api.py",
                    "tests/test_workqueue_commands.py",
                    "tests/test_workqueue_web.py",
                    "tests/playwright/e2e/test_workqueue.py",
                    "tests/architecture/test_agent_workqueue_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.admin.service_teams",
        "app.web.admin.workqueue",
        "app.api.workqueue",
        "app.services.support_ticket_settings",
        "app.services.team_inbox_*",
        "app.services.workqueue.*",
        "app.services.ticket_assignment.*",
        "app.services.ticket_work_order_handoff",
        "app.services.operational_escalation_delivery",
        "app.services.projects",
        "app.services.dispatch",
    ),
    rule="Party and staff-principal owners supply identity; the service-team "
    "owner supplies shared team topology and membership. Consumers "
    "translate Party membership to their current principal-facing "
    "identifiers through the owner's resolver and never write team rows "
    "or restore settings mirrors.",
)
