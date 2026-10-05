"""Canonical SOT declarations for the automation control plane."""

from __future__ import annotations

from app.services.automation_contracts import AutomationDomainCapabilities
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

DOMAIN = DomainSOT(
    domain="automation_control_plane",
    services=(
        SOTService(
            name="automation.script_runtime",
            module="app.services.automation_script_runtime",
            owns=("server-script runtime readiness policy",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="server-script runtime readiness policy",
                        role=OwnerRole.POLICY,
                        input_names=("deployment runtime image and digest",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="deployment runtime image and digest",
                        owner="automation.script_runtime",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="deployment-owned settings pinning the external OCI image and sha256 digest",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="pure readiness query; no database writes",
                    locking="not applicable",
                    idempotency="the same deployment settings produce the same readiness state",
                    retries="recheck after deployment configuration changes",
                ),
                errors=ErrorContract(
                    domain_codes=("automation.script_runtime.invalid_digest",),
                    mapping_owner="automation authoring adapters",
                    fail_closed_on=(
                        "missing image",
                        "missing digest",
                        "non-sha256 digest",
                    ),
                ),
                events=EventContract(
                    event_types=("automation.script_runtime_checked",),
                    schema_version=1,
                    delivery_owner="automation.capability_registry",
                    compatibility="Read-only deployment policy has no durable event.",
                    replay="Deployment settings are the current source of truth.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.script_runtime",
                    verification="runtime readiness and external-runner boundary tests",
                    cutover_gate="server-script publication refuses an unavailable or invalid runtime",
                    fallback_retirement="in-process script evaluation is not permitted",
                ),
                steward="platform automation",
                design_refs=(
                    "docs/adr/0005-external-connector-runtime.md",
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                ),
                test_refs=("tests/architecture/test_automation_runtime_boundary.py",),
            ),
        ),
        SOTService(
            name="automation.capability_registry",
            module="app.services.automation_capabilities",
            owns=("automation capability declarations and compatibility validation",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "automation capability declarations and compatibility "
                            "validation"
                        ),
                        role=OwnerRole.POLICY,
                        input_names=("SOT domain automation declarations",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="SOT domain automation declarations",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "immutable AutomationDomainCapabilities declared by each "
                            "canonical DomainSOT"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Pure in-process registry resolution; no database writes.",
                    locking="Not applicable because declarations are immutable code.",
                    idempotency="The same checked-in declarations resolve identically.",
                    retries="Retry the pure query after correcting an invalid manifest.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation_capability_undeclared",
                        "automation_capability_ambiguous",
                        "automation_capability_manifest_invalid",
                    ),
                    mapping_owner="automation authoring adapters",
                    fail_closed_on=(
                        "unknown module, trigger, action, target, or contract version",
                        "duplicate capability ownership",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.capability_registry",
                    verification="automation capability registry architecture tests",
                    cutover_gate=(
                        "only registered capabilities can be stored or executed by the "
                        "Automation Center"
                    ),
                    fallback_retirement="No runtime-editable capability registry exists.",
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/architecture/test_automation_capability_registry.py",
                ),
            ),
        ),
        SOTService(
            name="automation.rule_definitions",
            module="app.services.automation_rules",
            owns=("automation rule definitions and immutable versions",),
            depends_on=(
                "automation.capability_registry",
                "customer.search",
                "support.ticket_assignment_rule_configuration",
                "support.ticket_automation_rule_configuration",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="automation rule definitions and immutable versions",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed automation rule lifecycle command",
                            "declared automation capabilities",
                            "tenant-scoped automation rule records",
                            "company-wide or selected-customer rule scope",
                            "active ticket assignment rules",
                            "active ticket-creation automation rules",
                            "active Automation Center rules for the same trigger and action",
                        ),
                        canonical_writer="automation.rule_definitions",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed automation rule lifecycle command",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed create, replace-draft, publish, pause, resume, "
                            "and retire commands with actor and permission evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="declared automation capabilities",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "closed trigger, field, action, schema-version, permission, "
                            "and legacy-conflict declarations"
                        ),
                    ),
                    AuthorityInput(
                        name="tenant-scoped automation rule records",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AutomationRule and AutomationRuleVersion rows",
                    ),
                    AuthorityInput(
                        name="company-wide or selected-customer rule scope",
                        owner="customer.search",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "the exact selected set of currently active canonical "
                            "customer identities"
                        ),
                    ),
                    AuthorityInput(
                        name="active ticket assignment rules",
                        owner="support.ticket_assignment_rule_configuration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "current active TicketAssignmentRule rows used to reject an "
                            "overlapping Support Ticket assignment publication"
                        ),
                    ),
                    AuthorityInput(
                        name="active ticket-creation automation rules",
                        owner="support.ticket_automation_rule_configuration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "current active TicketAutomationRule rows used to reject an "
                            "overlapping Support Ticket action publication"
                        ),
                    ),
                    AuthorityInput(
                        name="active Automation Center rules for the same trigger and action",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "published AutomationRule active versions compared for shared "
                            "trigger, action, and conditions that cannot prove disjointness"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "each public lifecycle command enters execute_owner_command "
                        "once and commits its rule, version, and event atomically"
                    ),
                    locking=(
                        "tenant and rule identity are rechecked; existing rules and "
                        "draft versions are locked before mutation; current legacy and "
                        "central active-rule evidence is re-read before publication or resume"
                    ),
                    idempotency=(
                        "tenant rule keys, rule-version numbers, and one-draft indexes "
                        "converge duplicate commands"
                    ),
                    retries="retry the complete lifecycle command after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation.rule_definitions.not_found",
                        "automation.rule_definitions.permission_denied",
                        "automation.rule_definitions.identity_invalid",
                        "automation.rule_definitions.key_conflict",
                        "automation.rule_definitions.condition_field_undeclared",
                        "automation.rule_definitions.condition_operator_unsupported",
                        "automation.rule_definitions.condition_value_invalid",
                        "automation.rule_definitions.condition_contract_stale",
                        "automation.rule_definitions.condition_limit",
                        "automation.rule_definitions.customer_scope_invalid",
                        "automation.rule_definitions.action_inputs_invalid",
                        "automation.rule_definitions.action_input_duplicate",
                        "automation.rule_definitions.action_input_value_invalid",
                        "automation.rule_definitions.actions_required",
                        "automation.rule_definitions.action_contract_stale",
                        "automation.rule_definitions.action_order_invalid",
                        "automation.rule_definitions.action_schema_stale",
                        "automation.rule_definitions.action_duplicate",
                        "automation.rule_definitions.action_limit",
                        "automation.rule_definitions.active_version_missing",
                        "automation.rule_definitions.action_target_mismatch",
                        "automation.rule_definitions.trigger_runtime_unavailable",
                        "automation.rule_definitions.trigger_schema_stale",
                        "automation.rule_definitions.action_runtime_unavailable",
                        "automation.rule_definitions.legacy_scope_conflict",
                        "automation.rule_definitions.live_legacy_rule_conflict",
                        "automation.rule_definitions.active_rule_conflict",
                        "automation.rule_definitions.status_conflict",
                        "automation.rule_definitions.retired",
                        "automation.rule_definitions.draft_not_found",
                        *owner_command_boundary_error_codes(
                            "automation.rule_definitions"
                        ),
                    ),
                    mapping_owner="automation web and API adapters",
                    fail_closed_on=(
                        "unknown or stale capability contract",
                        "missing central or module permission",
                        "missing or inactive customer scope member",
                        "legacy-exclusive capability conflict",
                    ),
                ),
                events=EventContract(
                    event_types=("automation.rule_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries tenant, rule, version, status, and change "
                        "identity without rule values."
                    ),
                    replay=(
                        "Rule and immutable version rows reconstruct lifecycle state; "
                        "event-store rows reconstruct emitted transitions."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.rule_definitions",
                    verification=(
                        "automation rule owner, migration, permission, and architecture "
                        "tests"
                    ),
                    cutover_gate=(
                        "all Automation Center rule writes use typed owner commands"
                    ),
                    fallback_retirement=(
                        "no generic CRUD or direct ORM rule writer is admitted"
                    ),
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_automation_rules.py",
                    "tests/architecture/test_automation_rule_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="automation.execution",
            module="app.services.automation_runtime",
            owns=("automation execution decisions and run evidence",),
            depends_on=(
                "automation.capability_registry",
                "automation.rule_definitions",
                "events.store",
                "events.replay_evidence",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="automation execution decisions and run evidence",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "durable domain event evidence",
                            "durable event replay evidence",
                            "published automation rule versions",
                            "declared automation runtime adapters",
                            "tenant-scoped automation run evidence",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="durable domain event evidence",
                        owner="events.store",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "EventStore identity, type, payload, actor, and durable "
                            "handler retry evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="durable event replay evidence",
                        owner="events.replay_evidence",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "the typed replay envelope for the exact EventStore event "
                            "and expected event type"
                        ),
                    ),
                    AuthorityInput(
                        name="published automation rule versions",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active immutable AutomationRuleVersion conditions and "
                            "ordered action declarations"
                        ),
                    ),
                    AuthorityInput(
                        name="declared automation runtime adapters",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "closed code registry whose action keys exactly match "
                            "typed idempotent executors"
                        ),
                    ),
                    AuthorityInput(
                        name="tenant-scoped automation run evidence",
                        owner="automation.execution",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "AutomationRun, AutomationStepRun, and actor-attributed "
                            "AutomationRunRetry rows"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "event planning, each step claim, each module owner command, "
                        "and each step completion use separate committed owner "
                        "boundaries so external effects never share a transaction"
                    ),
                    locking=(
                        "rule-version/event uniqueness converges replay; run and step "
                        "rows are locked before claim and completion transitions; a "
                        "failed run is locked while one retry attempt is recorded"
                    ),
                    idempotency=(
                        "each action receives the stable event/version/step key and "
                        "module adapters must honor their declared idempotency contract"
                    ),
                    retries=(
                        "an administrator retry is actor-attributed and pinned to the "
                        "original rule version; succeeded steps are skipped, failed "
                        "steps are retried, and expired claims are reclaimed"
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation.execution.trigger_event_mismatch",
                        "automation.execution.trigger_target_mismatch",
                        "automation.execution.step_not_found",
                        "automation.execution.run_not_found",
                        "automation.execution.run_rule_not_found",
                        "automation.execution.action_failed",
                        "automation.execution.event_handler_failed",
                        "automation.execution.blocked_after_failure",
                        "automation.execution.run_not_retryable",
                        "automation.execution.run_version_unavailable",
                        "automation.execution.run_has_no_retryable_steps",
                        "automation.execution.retry_not_found",
                        "automation.execution.retry_command_conflict",
                        "automation.execution.retry_trigger_mismatch",
                        "automation.execution.retry_event_identity_invalid",
                        "automation.execution.retry_target_mismatch",
                        "automation.execution.retry_event_mismatch",
                        "automation.execution.retry_trigger_unavailable",
                        "automation.execution.retry_runtime_unavailable",
                        "automation.execution.retry_event_type_invalid",
                        "automation.execution.step_busy",
                        "automation.execution.retry_failed",
                        "automation.execution.run_history_query_invalid",
                        *owner_command_boundary_error_codes("automation.execution"),
                    ),
                    mapping_owner="automation event and web adapters",
                    fail_closed_on=(
                        "missing or mismatched event tenant and target identity",
                        "capability-to-executor registry mismatch",
                        "busy or failed ordered action step",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.execution",
                    verification=(
                        "automation run detail, actor-attributed retry, handler "
                        "registration, RLS, replay, and static adapter registry tests"
                    ),
                    cutover_gate=(
                        "a module receives no automation traffic until its trigger, "
                        "action, and exact executor are admitted together"
                    ),
                    fallback_retirement=(
                        "remove the module declaration and executor; existing legacy "
                        "automation surfaces remain independently owned"
                    ),
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_automation_runtime.py",
                    "tests/test_event_replay_evidence.py",
                    "tests/architecture/test_automation_runtime_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="automation.script_definitions",
            module="app.services.automation_scripts",
            owns=(
                "automation script definitions and immutable versions",
                "automation script execution evidence",
            ),
            depends_on=("automation.capability_registry",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="automation script definitions and immutable versions",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed script lifecycle command",
                            "declared script targets",
                            "tenant-scoped script identity and immutable source versions",
                        ),
                        canonical_writer="automation.script_definitions",
                    ),
                    ConcernContract(
                        name="automation script execution evidence",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "published script version",
                            "durable source-free execution outcome",
                        ),
                        canonical_writer="automation.script_definitions",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed script lifecycle command",
                        owner="automation.script_definitions",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="typed create-draft, edit-draft, publish, and lifecycle commands with actor, permission, target, event, runtime, and source-hash evidence",
                    ),
                    AuthorityInput(
                        name="declared script targets",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="closed client/server target and event declarations in canonical domain SOT modules",
                    ),
                    AuthorityInput(
                        name="tenant-scoped script identity and immutable source versions",
                        owner="automation.script_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AutomationScript and AutomationScriptVersion rows",
                    ),
                    AuthorityInput(
                        name="published script version",
                        owner="automation.script_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="the tenant-scoped published script and active immutable version",
                    ),
                    AuthorityInput(
                        name="durable source-free execution outcome",
                        owner="automation.script_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AutomationScriptRun status, result code, and error code without source or payload",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="each script lifecycle command enters execute_owner_command once; ORM writes are flush-only inside the command",
                    locking="tenant/key identity and the selected script version are locked before mutation",
                    idempotency="tenant and script key uniqueness rejects ambiguous creation; edit preserves one draft, and publish selects that immutable version while retaining its source hash",
                    retries="retry the complete lifecycle command after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation.script_definitions.permission_denied",
                        "automation.script_definitions.identity_invalid",
                        "automation.script_definitions.target_undeclared",
                        "automation.script_definitions.event_undeclared",
                        "automation.script_definitions.source_empty",
                        "automation.script_definitions.source_too_large",
                        "automation.script_definitions.source_uses_forbidden_api",
                        "automation.script_definitions.key_conflict",
                        "automation.script_definitions.not_found",
                        "automation.script_definitions.retired",
                        "automation.script_definitions.version_missing",
                        "automation.script_definitions.active_version_missing",
                        "automation.script_definitions.invalid_transition",
                        "automation.script_definitions.runtime_unavailable",
                        "automation.script_definitions.run_not_found",
                        *owner_command_boundary_error_codes(
                            "automation.script_definitions"
                        ),
                    ),
                    mapping_owner="automation web authoring adapters",
                    fail_closed_on=(
                        "unknown target or event",
                        "forbidden runtime API",
                        "duplicate script identity",
                    ),
                ),
                events=EventContract(
                    event_types=("automation.script_changed",),
                    schema_version=1,
                    delivery_owner="automation.execution",
                    compatibility="Version 1 records script identity, version, target, event, and lifecycle change without source code.",
                    replay="AutomationScript and AutomationScriptVersion rows reconstruct the authoring state.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.script_definitions",
                    verification="script control-plane migration, target validation, hash, and RLS tests",
                    cutover_gate="all script definitions are created and published through typed owner commands",
                    fallback_retirement="no editable source-code column exists outside immutable script versions",
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/architecture/test_automation_capability_registry.py",
                ),
            ),
        ),
        SOTService(
            name="automation.scheduled_runs",
            module="app.services.automation_scheduled",
            owns=("scheduled automation run claims",),
            depends_on=(
                "automation.rule_definitions",
                "automation.capability_registry",
                "events.store",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="scheduled automation run claims",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "published scheduled automation rules",
                            "scheduled automation claim rows",
                        ),
                        canonical_writer="automation.scheduled_runs",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="published scheduled automation rules",
                        owner="automation.rule_definitions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="published AutomationRuleVersion schedule declarations",
                    ),
                    AuthorityInput(
                        name="scheduled automation claim rows",
                        owner="automation.scheduled_runs",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="AutomationScheduledRun rows keyed by rule version and schedule slot",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="enqueue_scheduled_events owns one transaction for due slot claims and their durable target events; the task owns session lifecycle only",
                    locking="the unique rule-version and slot identity rejects concurrent claims",
                    idempotency="the same rule version and slot converge on one scheduled-run claim",
                    retries="a later sweep may claim an unclaimed or expired slot after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation.scheduled_runs.invalid_schedule_time",
                        *owner_command_boundary_error_codes(
                            "automation.scheduled_runs"
                        ),
                    ),
                    mapping_owner="automation task adapter",
                    fail_closed_on=("duplicate or invalid scheduled slot identity",),
                ),
                events=EventContract(
                    event_types=("custom",),
                    schema_version=1,
                    delivery_owner="events.store",
                    compatibility="Custom target events name the claimed rule version and tenant; ordinary event-driven envelopes remain unchanged.",
                    replay="AutomationScheduledRun rows reconstruct claim state for a schedule slot.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.scheduled_runs",
                    verification="scheduled-run migration and scheduler claim tests",
                    cutover_gate="scheduled execution claims use the dedicated scheduled-run owner",
                    fallback_retirement="no task or web adapter writes scheduled-run rows directly",
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_automation_scheduled.py",
                    "tests/test_automation_runtime.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.services.automation_capabilities",
        "app.services.automation_rules",
        "app.services.automation_runtime",
        "app.services.automation_script_runtime",
        "app.services.automation_scripts",
        "app.services.events.handlers.automation",
        "app.services.web_automation_center",
        "app.web.admin.automation_center",
    ),
    rule=(
        "Every SOT domain is visible to the Automation Center, but only a closed, "
        "versioned domain declaration may expose executable triggers or actions."
    ),
    automation=AutomationDomainCapabilities(target_types=("automation.rule",)),
)
