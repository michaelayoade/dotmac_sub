"""Read composition concerns belonging to the existing field-jobs owner."""

from dataclasses import replace

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    ConcernContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
)

BRIEF_DEPENDENCIES = (
    "operations.work_order_templates",
    "network.outage_lifecycle",
    "network.outage_work_order_handoff",
    "network.identity",
    "network.infrastructure_catalogue",
    "operations.project_work_order_handoff",
    "network.fiber_topology",
    "customer.accounts",
    "operations.project_lifecycle",
    "operations.field_completion",
)
BRIEF_CONCERNS = (
    "assignment-scoped typed field job brief composition",
    "field-authorized issued and current outage audience presentation",
    "current-assignment explicit vendor job destinations",
)
_BRIEF_INPUTS = (
    AuthorityInput(
        "explicit stored work classification and versioned recommendation",
        "operations.work_order_templates",
        AuthorityKind.CONTROL_INPUT,
        "Explicit stored purpose/access technology/source/version; legacy unclassified or stale saved version has no guessed/replaced recommendation.",
    ),
    AuthorityInput(
        "linked outage work-order provenance",
        "network.outage_work_order_handoff",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "OutageIncidentWorkOrderLink for the currently assigned native work order; issued scope/revision/token stay immutable.",
    ),
    AuthorityInput(
        "immutable outage audience revisions",
        "network.outage_lifecycle",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Canonical latest_scope_revision and revision_audience_subscription_ids; entered/retained members, exact count and membership token.",
    ),
    AuthorityInput(
        "field customer operational identity",
        "customer.accounts",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Explicit subscription/customer/service-address allowlist; deleted subscription membership survives as unavailable details.",
    ),
    AuthorityInput(
        "linked native project execution task",
        "operations.project_lifecycle",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Only the native work-order project/task relationship, task status/instructions and generation provenance; no sibling-project expansion.",
    ),
    AuthorityInput(
        "explicit native project infrastructure target",
        "operations.project_lifecycle",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Typed engineering_plan_origin current active Project/Task infrastructure reference for this native job; issued jobs additionally require exact immutable receipt target agreement. Only target coordinates are projected, never surrounding inventory.",
    ),
    AuthorityInput(
        "immutable project job issuance target",
        "operations.project_work_order_handoff",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "ProjectWorkOrderIssuance exact job/project/native infrastructure target; changed current target cannot expand an issued job's map scope.",
    ),
    AuthorityInput(
        "field completion requirement projection",
        "operations.field_completion",
        AuthorityKind.CONTROL_INPUT,
        "Existing completion requirement resolver; recommendations and task status never authorize completion.",
    ),
    AuthorityInput(
        "exact issued outage target coordinates",
        "network.identity",
        AuthorityKind.AUTHORITATIVE_RECORD,
        "Exact issued target ID and current native plant/site coordinate allowlist. No proximity inference or node-to-site guessed pin.",
    ),
)


def with_job_brief_contract(base: ServiceContract) -> ServiceContract:
    input_names = tuple(
        item.name for item in (*base.authoritative_inputs, *_BRIEF_INPUTS)
    )
    return replace(
        base,
        concerns=(
            *base.concerns,
            *(
                ConcernContract(name, OwnerRole.RESOLVER, input_names)
                for name in BRIEF_CONCERNS
            ),
        ),
        authoritative_inputs=(*base.authoritative_inputs, *_BRIEF_INPUTS),
        errors=replace(
            base.errors,
            domain_codes=(
                *base.errors.domain_codes,
                "operations.field_jobs.brief.invalid_page",
                "operations.field_jobs.brief.not_found",
                *(
                    f"operations.field_jobs.outage_audience.{code}"
                    for code in (
                        "invalid_page",
                        "revision_required",
                        "not_found",
                        "issued_revision_missing",
                        "inconsistent_revision",
                        "inconsistent_link",
                        "revision_changed",
                    )
                ),
            ),
        ),
        projections=(
            *base.projections,
            ProjectionContract(
                name="current-assignment explicit vendor job destinations",
                input_names=input_names,
                writer="operations.field_jobs",
                freshness="Every request resolves the active native member and exact current selected job; no vendor global plant cache or device-position disclosure.",
                stale_behavior="Reassignment, membership revocation and unavailable explicit relationships refuse or omit destinations. Vendor queries never infer relationship from proximity. Staff's existing nearby destinations remain separate technician-plane behavior.",
                drift_signal="Direct vendor global-map bypass, unrelated nearby plant, current selected-member, explicit outage/project target and revoked-assignment tests.",
                rebuild_operation="Refresh the current authorized job and call list_destinations using its FieldJobQuery.",
                repair_owner="operations.field_jobs",
            ),
            ProjectionContract(
                name=BRIEF_CONCERNS[0],
                input_names=input_names,
                writer="operations.field_jobs",
                freshness="Recomputed on each authorized request; latest audience means latest persisted immutable revision, not live topology.",
                stale_behavior="Project target is exact, no guessed node/site coordinates; vendor destinations consume these explicit directions only. Issued and current revisions are explicit. Unknown scope has no numeric zero count. Continuation refuses a changed revision; missing/corrupt issued evidence fails closed.",
                drift_signal="Revision member count/token mismatch, link provenance mismatch, changed continuation revision, or unavailable subscription identity.",
                rebuild_operation="Call get_job_brief with the same actor/work-order identity; restart pagination on the first page of the selected revision.",
                repair_owner="network.outage_lifecycle",
            ),
        ),
        design_refs=(
            *base.design_refs,
            "docs/designs/FIELD_JOB_BRIEF.md",
            "docs/designs/VENDOR_JOB_MAP_PRIVACY.md",
        ),
        test_refs=(
            *base.test_refs,
            "tests/test_field_job_brief.py",
            "tests/test_vendor_job_map_scope.py",
            "tests/architecture/test_field_job_brief_boundary.py",
        ),
    )
