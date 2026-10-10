"""Composable captive access policy, router gate, and policy-change ownership."""

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

POLICY = "access.captive_access_policy"
ROUTER_GATE = "access.captive_router_gate"
POLICY_CHANGE = "access.captive_access_policy_change"

_DESIGN_REFS = (
    "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
    "docs/SOT_RELATIONSHIP_MAP.md",
)

POLICY_SERVICE = SOTService(
    name=POLICY,
    module="app.services.captive_access_policy",
    owns=(
        "captive access rules and customer sets",
        "per-subscription captive policy resolution",
    ),
    depends_on=(
        "customer.accounts",
        "customer.identity_scope",
        "service_intent.catalog_policy",
        "observability.audit_log",
        "events.dispatcher",
    ),
    notes=(
        "Typed rules scoped global, plan_family (optional offer ids), "
        "customer_set (named audited cohort) or account, each allow/deny with "
        "optional subscriber-category and house/specific-reseller conditions. "
        "Resolution is per subscription: account > customer_set > plan_family "
        "> global, deny beats allow at the same scope, default deny. Records "
        "are written only through the policy-change coordinator; "
        "Subscriber.captive_redirect_enabled is a retired, readable column."
    ),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name="captive access rules and customer sets",
                role=OwnerRole.COMMAND_WRITER,
                canonical_writer=POLICY,
                input_names=(
                    "reviewed captive policy change",
                    "canonical subscriber identity",
                    "canonical reseller scope",
                ),
            ),
            ConcernContract(
                name="per-subscription captive policy resolution",
                role=OwnerRole.RESOLVER,
                input_names=(
                    "captive policy records",
                    "canonical subscriber identity",
                    "canonical reseller scope",
                    "canonical catalog offer family",
                ),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="reviewed captive policy change",
                owner=POLICY_CHANGE,
                kind=AuthorityKind.CONTROL_INPUT,
                source=(
                    "typed CaptivePolicyChange validated inside the "
                    "policy-change owner command"
                ),
            ),
            AuthorityInput(
                name="captive policy records",
                owner=POLICY,
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source=(
                    "enabled captive_access_rules and open "
                    "captive_customer_set_members of active captive_customer_sets"
                ),
            ),
            AuthorityInput(
                name="canonical subscriber identity",
                owner="customer.accounts",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Subscriber id and explicit subscriber_category metadata",
            ),
            AuthorityInput(
                name="canonical reseller scope",
                owner="customer.identity_scope",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Reseller id, is_active and is_house",
            ),
            AuthorityInput(
                name="canonical catalog offer family",
                owner="service_intent.catalog_policy",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="Subscription.offer_id and CatalogOffer.plan_family",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.PARTICIPANT,
            boundary=(
                "Resolution is read-only. Writes are flush-only and refuse to run "
                "outside the access.captive_access_policy_change owner command, "
                "which commits them together with lock re-evaluation."
            ),
            locking=(
                "Disabling a rule locks that rule row; membership changes lock "
                "the customer-set row and its open membership rows. The "
                "coordinator serializes changes with a transaction advisory lock."
            ),
            idempotency=(
                "Exact duplicate enabled rules and duplicate set names are "
                "refused; adding an existing member or removing a non-member is "
                "a counted no-op. Command replay is owned by the coordinator."
            ),
            retries=(
                "No independent retry; the coordinator re-previews after a "
                "refusal or stale state."
            ),
        ),
        errors=ErrorContract(
            domain_codes=(
                "access.captive_access_policy.invalid_change",
                "access.captive_access_policy.rule_not_found",
                "access.captive_access_policy.customer_set_not_found",
                "access.captive_access_policy.subscriber_not_found",
                "access.captive_access_policy.duplicate_rule",
                "access.captive_access_policy.duplicate_customer_set",
                "access.captive_access_policy.outside_coordinator",
            ),
            mapping_owner="scripts.network.captive_access_policy",
            fail_closed_on=(
                "no matching rule (default deny)",
                "a deny at the most specific matching scope",
                "unclassified accounts against a category condition",
                "malformed persisted ids, which never match",
                "writes outside the policy-change coordinator",
            ),
        ),
        events=EventContract(
            event_types=("captive_access_policy.changed",),
            schema_version=1,
            delivery_owner="events.dispatcher",
            compatibility=(
                "Additive payload: action, entity type and id, change kind, "
                "command id. No customer identity."
            ),
            replay=(
                "Informational; consumers must be idempotent. Lock consequences "
                "travel on enforcement_lock.access_mode_changed."
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.CUT_OVER,
            old_owner="per-account Subscriber.captive_redirect_enabled flag",
            new_owner=POLICY,
            verification=(
                "Revision 666 converts every opt-in into one account allow rule "
                "with the former residential/house conditions and verifies the "
                "count; precedence, rail, gate and migration tests."
            ),
            cutover_gate=(
                "access.walled_garden_policy consumes only the resolver; no "
                "decision path reads captive_redirect_enabled (architecture "
                "guard) and the admin form writer is removed."
            ),
            fallback_retirement=(
                "The column stays readable for one release for rollback; a later "
                "contract revision drops it with SubscriberRead's field."
            ),
        ),
        steward="network access",
        design_refs=_DESIGN_REFS,
        test_refs=(
            "tests/test_captive_access_policy.py",
            "tests/architecture/test_captive_access_policy_boundary.py",
        ),
    ),
)

ROUTER_GATE_SERVICE = SOTService(
    name=ROUTER_GATE,
    module="app.services.captive_router_gate",
    owns=("serving-router captive readiness gate",),
    depends_on=(
        "access.walled_garden_router_readiness",
        "sessions.radius_reconciliation",
        "network.nas_inventory",
        "network.identity",
    ),
    notes=(
        "Serving NAS = provisioning_nas_device_id plus every radius_active_"
        "sessions row of the subscription (or unbound rows of its account), "
        "mapped by nas_device_id or a unique NAS IP; routers via "
        "routers.nas_device_id. Passes only when every serving router is "
        "ready. Per-run cache; never contacts a router."
    ),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name="serving-router captive readiness gate",
                role=OwnerRole.RESOLVER,
                input_names=(
                    "subscription NAS assignment",
                    "active RADIUS session projection",
                    "NAS device inventory",
                    "router inventory identity",
                    "per-router walled-garden readiness",
                ),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="subscription NAS assignment",
                owner="service_intent.subscription_nas_assignment",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="subscriptions.provisioning_nas_device_id",
            ),
            AuthorityInput(
                name="active RADIUS session projection",
                owner="sessions.radius_reconciliation",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source=(
                    "radius_active_sessions nas_device_id / nas_ip_address for "
                    "the subscription and unbound rows of its account"
                ),
            ),
            AuthorityInput(
                name="NAS device inventory",
                owner="network.nas_inventory",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="nas_devices id, nas_ip, ip_address",
            ),
            AuthorityInput(
                name="router inventory identity",
                owner="network.identity",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="active routers rows and routers.nas_device_id",
            ),
            AuthorityInput(
                name="per-router walled-garden readiness",
                owner="access.walled_garden_router_readiness",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source="resolve_routers_walled_garden_readiness (snapshot-derived)",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.READ_ONLY,
            boundary=(
                "Caller creates and closes the session; the gate reads inventory, "
                "sessions and snapshots without writes."
            ),
            locking="none",
            idempotency=(
                "The same assignment, session rows, inventory, snapshots and "
                "evaluation instant produce the same typed decision."
            ),
            retries=(
                "Transient reads may be retried; missing or ambiguous evidence is "
                "a typed fail-closed outcome, not an error."
            ),
        ),
        errors=ErrorContract(
            domain_codes=(),
            mapping_owner="access.walled_garden_policy",
            fail_closed_on=(
                "no serving NAS evidence",
                "a session NAS that cannot be identified or is ambiguous",
                "a serving NAS without an active router",
                "any serving router not ready (stale, no snapshot, findings, "
                "not configured)",
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner=ROUTER_GATE,
        ),
        steward="network access",
        design_refs=_DESIGN_REFS,
        test_refs=(
            "tests/test_captive_router_gate.py",
            "tests/architecture/test_captive_access_policy_boundary.py",
        ),
    ),
)

POLICY_CHANGE_SERVICE = SOTService(
    name=POLICY_CHANGE,
    module="app.services.captive_access_policy_change",
    owns=(
        "captive access policy change and lock re-evaluation",
        "captive access policy change preview",
    ),
    depends_on=(
        POLICY,
        ROUTER_GATE,
        "access.walled_garden_policy",
        "access.subscription_lifecycle",
        "auth.permission_gate",
        "observability.audit_log",
    ),
    notes=(
        "Preview evaluates a candidate change against every subscription with "
        "an active lock that requested captive and reports lock updates and "
        "hard_reject<->captive moves by router and plan family, with an exact "
        "fingerprint. Apply re-derives the plan, refuses a stale fingerprint, "
        "writes the change, verifies the written policy reproduces the plan, "
        "and re-evaluates lock access modes through the lifecycle owner for a "
        "bounded batch. Changed locks emit enforcement_lock.access_mode_changed; "
        "the enforcement handler reprojects RADIUS and enqueues session cleanup "
        "after commit."
    ),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name="captive access policy change and lock re-evaluation",
                role=OwnerRole.APPLICATION_COORDINATOR,
                input_names=(
                    "attributable policy change command",
                    "captive policy records",
                    "canonical enforcement locks",
                    "effective captive restriction",
                    "staff permission grants",
                ),
            ),
            ConcernContract(
                name="captive access policy change preview",
                role=OwnerRole.RESOLVER,
                input_names=(
                    "captive policy records",
                    "canonical enforcement locks",
                    "effective captive restriction",
                ),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="attributable policy change command",
                owner=POLICY_CHANGE,
                kind=AuthorityKind.CONTROL_INPUT,
                source=(
                    "typed change, exact preview fingerprint, batch bound, "
                    "RBAC-verified staff principal, reason and idempotency key"
                ),
            ),
            AuthorityInput(
                name="captive policy records",
                owner=POLICY,
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="captive_access_rules and captive customer sets",
            ),
            AuthorityInput(
                name="canonical enforcement locks",
                owner="access.subscription_lifecycle",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source=("active EnforcementLock access_mode and requested_access_mode"),
            ),
            AuthorityInput(
                name="effective captive restriction",
                owner="access.walled_garden_policy",
                kind=AuthorityKind.DERIVED_PROJECTION,
                source="resolve_walled_garden_decision per subscription",
            ),
            AuthorityInput(
                name="staff permission grants",
                owner="auth.permission_gate",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="network:radius:write via has_permission, re-read in-command",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.COORDINATOR_MANAGED,
            boundary=(
                "One owner command: replay check, permission, re-preview and "
                "fingerprint check, policy write, verification, lifecycle lock "
                "updates for at most max_subscriptions subscriptions, audit, "
                "events, and the idempotency row commit together. Preview is "
                "read-only."
            ),
            locking=(
                "Transaction advisory lock serializes policy changes; the "
                "lifecycle participant locks subscriptions then enforcement "
                "locks in id order and compare-and-sets each expected mode."
            ),
            idempotency=(
                "Unique idempotency_key; the same change and preview fingerprint "
                "replays the stored outcome, any other reuse is refused."
            ),
            retries=(
                "A stale preview or lock is refused; the operator previews again. "
                "Remaining batches are drained by re-running a reevaluate change."
            ),
        ),
        errors=ErrorContract(
            domain_codes=(
                "access.captive_access_policy_change.invalid_command",
                "access.captive_access_policy_change.permission_denied",
                "access.captive_access_policy_change.stale_preview",
                "access.captive_access_policy_change.idempotency_conflict",
                "access.captive_access_policy_change.apply_diverged_from_preview",
                "access.subscription_lifecycle.lock_access_mode_stale",
                "access.subscription_lifecycle.lock_access_mode_exceeds_request",
                *owner_command_boundary_error_codes(POLICY_CHANGE),
            ),
            mapping_owner="scripts.network.captive_access_policy",
            fail_closed_on=(
                "stale preview fingerprint",
                "a lock whose mode changed after the preview",
                "captive above a lock's requested treatment",
                "unauthorized or inactive staff principal",
                "written policy that does not reproduce the preview",
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner=POLICY_CHANGE,
        ),
        steward="network access",
        design_refs=_DESIGN_REFS,
        test_refs=(
            "tests/test_captive_access_policy_change.py",
            "tests/architecture/test_captive_access_policy_boundary.py",
        ),
    ),
)

SERVICES = (POLICY_SERVICE, ROUTER_GATE_SERVICE, POLICY_CHANGE_SERVICE)
