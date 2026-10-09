"""sales_referrals SOT declarations: lifecycle."""

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
        name="sales.fiber_feasibility",
        module="app.services.sales.fiber_feasibility",
        owns=("initial Fiber location feasibility",),
        depends_on=("network.fiber_asset_changes", "control.settings_spec"),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="initial Fiber location feasibility",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "installation pin",
                        "active Fiber access points",
                        "effective feasibility radius",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="installation pin",
                    owner="sales.fiber_feasibility",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="Validated Decimal latitude/longitude in FiberFeasibilityQuery.",
                ),
                AuthorityInput(
                    name="active Fiber access points",
                    owner="network.fiber_asset_changes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Active FiberAccessPoint geometry and identity; KNN selection and existing EPSG:3857 distance.",
                ),
                AuthorityInput(
                    name="effective feasibility radius",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="selfserve_quote_feasibility_radius_meters, with the existing 2000m fallback.",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Flush-free read inside the caller's transaction; optional coverage callers isolate it through execute_owner_savepoint.",
                locking="No write locks; current statement snapshot.",
                idempotency="The same geometry, pin and radius produce the same initial indication.",
                retries="Safe to repeat the read after the transaction owner restores a failed optional savepoint.",
            ),
            errors=ErrorContract(
                domain_codes=("sales.fiber_feasibility.invalid_coordinates",),
                mapping_owner="Quote and Fiber inquiry adapters/coordinators",
                fail_closed_on=(
                    "invalid coordinates",
                    "database or PostGIS query failure",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="sales.selfserve.compute_feasibility",
                new_owner="sales.fiber_feasibility",
                verification="Typed consumer, KNN and PostgreSQL failed-query retention tests.",
                cutover_gate="All production feasibility consumers use the same typed query/result.",
                fallback_retirement="The old primitive-bag function and parallel nearest-point query are removed.",
            ),
            steward="sales platform",
            design_refs=(
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
                "docs/runbooks/FIBER_ACQUISITION_ATTRIBUTION.md",
            ),
            test_refs=(
                "tests/test_sales_selfserve.py",
                "tests/test_sales_selfserve_perf.py",
                "tests/integration/test_fiber_coverage_failure_retention.py",
                "tests/architecture/test_fiber_feasibility_boundary.py",
            ),
            projections=(
                ProjectionContract(
                    name="initial Fiber indication",
                    input_names=(
                        "installation pin",
                        "active Fiber access points",
                        "effective feasibility radius",
                    ),
                    writer="sales.fiber_feasibility",
                    freshness="Recomputed on each request; recorded receipt results retain their original indication on replay.",
                    stale_behavior="An indication is not an installation guarantee; staff confirm network capacity and access.",
                    drift_signal="Changed plant geometry or radius requires a fresh enquiry/assessment, not rewriting the receipt.",
                    rebuild_operation="assess with FiberFeasibilityQuery",
                    repair_owner="sales.fiber_feasibility",
                ),
            ),
        ),
    ),
    SOTService(
        name="sales.lifecycle_reconciliation",
        module="app.services.sales_lifecycle_reconciliation",
        owns=("sales-to-service projection drift repair orchestration",),
        depends_on=(
            "sales.fulfillment",
            "operations.service_order_lifecycle",
            "customer.experience_handoff",
        ),
        notes=(
            "The reconciler invents no identity, receipt, implementation "
            "verification, provisioning result, or customer acceptance."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="sales-to-service projection drift repair orchestration",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical SalesOrder delivery state",
                        "canonical vendor verification evidence",
                        "canonical ServiceOrder delivery state",
                        "canonical CX handoff state",
                    ),
                    canonical_writer="sales.lifecycle_reconciliation",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical SalesOrder delivery state",
                    owner="sales.orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "all active non-cancelled SalesOrders and their structural "
                        "Project relationships in deterministic order"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor verification evidence",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "verified InstallationProjects and latest append-only "
                        "verification events"
                    ),
                ),
                AuthorityInput(
                    name="canonical ServiceOrder delivery state",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "structurally linked ServiceOrders, implementation event ids, "
                        "and active provisioning outcomes"
                    ),
                ),
                AuthorityInput(
                    name="canonical CX handoff state",
                    owner="customer.experience_handoff",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "structurally linked CustomerExperienceHandoff roots and "
                        "append-only transition evidence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Dry-run reads then rolls back; apply invokes canonical owners "
                    "with commit disabled and commits all selected repairs once."
                ),
                locking=(
                    "Each repair owner locks its exact SalesOrder, installation, "
                    "Project, ServiceOrder, or handoff root before mutation."
                ),
                idempotency=(
                    "Only missing structural projections are requested; canonical "
                    "owner constraints and evidence ids make repeat apply a no-op."
                ),
                retries=(
                    "A failed repair rolls back and the operator replays the same "
                    "deterministic scan from authoritative inputs."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "sales.lifecycle_reconciliation.active_caller_transaction",
                    "sales.lifecycle_reconciliation.command_contract_violation",
                    "sales.lifecycle_reconciliation.invalid_command_context",
                    "sales.lifecycle_reconciliation.nested_owner_command",
                    "sales.lifecycle_reconciliation.nested_transaction_completion",
                    "implementation_scope_repair_rejected",
                    "verified_release_repair_rejected",
                    "cx_handoff_repair_rejected",
                ),
                mapping_owner="sales lifecycle reconciliation command adapter",
                retryable_codes=(
                    "implementation_scope_repair_rejected",
                    "verified_release_repair_rejected",
                    "cx_handoff_repair_rejected",
                ),
                fail_closed_on=(
                    "missing verification evidence",
                    "structural identity conflict",
                    "owner-rejected transition",
                ),
            ),
            events=EventContract(
                event_types=(
                    "project.created",
                    "installation_scope.created",
                    "implementation.released",
                    "service_order.released",
                    "customer_experience.ready",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "The reconciler emits no bespoke facts; version 1 owner events "
                    "retain their original schemas and exact identifiers."
                ),
                replay=(
                    "Re-running the deterministic scan requests only still-missing "
                    "projections through their canonical owner."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="sales-to-service lifecycle convergence",
                    input_names=(
                        "canonical SalesOrder delivery state",
                        "canonical vendor verification evidence",
                        "canonical ServiceOrder delivery state",
                        "canonical CX handoff state",
                    ),
                    writer="sales.lifecycle_reconciliation",
                    freshness="evaluated from current rows on every operator run",
                    stale_behavior=(
                        "report exact missing scopes, releases, evidence, or handoffs "
                        "without inferring a business fact"
                    ),
                    drift_signal=(
                        "nonzero missing_implementation_scope, "
                        "verified_implementation_not_released, "
                        "verified_implementation_missing_evidence, or "
                        "active_service_orders_without_cx_handoff counts"
                    ),
                    rebuild_operation=(
                        "reconcile_sales_to_service_lifecycle(apply=True)"
                    ),
                    repair_owner="sales.lifecycle_reconciliation",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "partial customer lifecycle audit without implementation, "
                    "provisioning, or CX convergence repair"
                ),
                new_owner="sales.lifecycle_reconciliation",
                verification=(
                    "Dry-run, owner-backed apply, missing evidence, idempotency, and "
                    "end-to-end lifecycle tests."
                ),
                cutover_gate=(
                    "Every repair delegates to the same production owner and no "
                    "reconciler invents money, identity, verification, or acceptance."
                ),
                fallback_retirement=(
                    "Memo inference, direct row patching, and CRM fallback are absent."
                ),
            ),
            steward="sales and service delivery",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_sales_to_service_lifecycle.py",
                "tests/test_sales_lifecycle_migration.py",
                "tests/test_sot_relationships.py",
            ),
        ),
    ),
    SOTService(
        name="sales.selfserve",
        module="app.services.sales.selfserve",
        owns=("self-serve quote and signup flow",),
        depends_on=(
            "sales.fiber_feasibility",
            "communications.intents",
            "communications.staff_notifications",
            "events.dispatcher",
            "sales.quote_payment_eligibility",
            "sales.quote_payment_review",
            "sales.service",
        ),
        notes=(
            "A map-pinned installation or relocation request records the typed "
            "service choice and is created as payment-review pending. Fiber "
            "installation may have an internal preliminary estimate; other "
            "choices require staff pricing. Customer reads hide prices until "
            "the current commercial snapshot is approved. Customer Quote reads "
            "use canonical paid deposit Invoice evidence from "
            "sales.quote_payment_eligibility so paid Quotes suppress payment "
            "actions. The owner queues staff review and customer under-review "
            "notifications."
        ),
    ),
    SOTService(
        name="sales.lead_lifecycle",
        module="app.services.sales.lifecycle",
        owns=(
            "Party-first Lead identity lifecycle",
            "immutable structured Lead origin capture",
            "reviewed Lead to Subscriber account attachment",
            "Lead-to-Quote and Lead-to-Ticket Party alignment",
        ),
        depends_on=("party.registry", "communications.campaigns"),
        notes=(
            "Native Sub campaign responses and external ad-provider "
            "identifiers are deliberately distinct. dotmac_mkt and CRM "
            "have no lead, customer, attribution, or lifecycle authority."
        ),
    ),
    SOTService(
        name="sales.service",
        module="app.services.sales.service",
        owns=(
            "sales pipeline and quote lifecycle",
            "governed pipeline stage presentation and ordering",
            "read-only sales pipeline reporting",
        ),
        depends_on=("sales.lead_lifecycle",),
        notes=(
            "The typed Lead and Quote list queries normalize search, filters, "
            "sort, and pagination once. Their row and count projections share "
            "one predicate specification; related Party, active contact-point, "
            "and Subscriber matches use correlated EXISTS predicates so JSON-"
            "bearing Lead and Quote rows are never subjected to full-row DISTINCT. "
            "Lead creation dates are normalized by normalize_lead_date_range; "
            "the same inclusive UTC scope supplies rows, count, summary, and retry. "
            "Quote dates likewise use public normalize_quote_date_range for query "
            "and retry; invalid or unrepresentable bounds become All time."
        ),
    ),
)
