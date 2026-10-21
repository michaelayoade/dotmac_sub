"""Protected interpretation of actual shared scope; original record owners persist."""

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

INPUTS = (
    AuthorityInput(
        name="immutable outage lifecycle evidence",
        owner="network.outage_lifecycle",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Incident state/resolved_at, immutable audience revisions/members and native complaint correlation receipts, including explicit reviewed_adoption provenance. SQL child-creation guards compare immutable server-stamped full xid8 creation_outer_xid with pg_current_xact_id(), valid across owner savepoints; no inferred historical customer or service proof.",
    ),
    AuthorityInput(
        name="actual outage field issuance",
        owner="network.outage_work_order_handoff",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Exact immutable OutageIncidentWorkOrderLink identity/revision/token/target and canonical infrastructure Ticket; all audience-matching actual issued scopes are required.",
    ),
    AuthorityInput(
        name="current complaint native customer",
        owner="support.ticket_lifecycle",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Fresh locked Ticket active/unmerged customer identity; no runtime lifecycle command dependency.",
    ),
    AuthorityInput(
        name="current selected service binding",
        owner="access.subscription_lifecycle",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Globally sorted selected Subscription row locks and fresh exact subscriber equality.",
    ),
    AuthorityInput(
        name="issued work header facts",
        owner="operations.work_orders",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Exact canonical public ID/origin/project/task/native infrastructure classification; no fake subscriber.",
    ),
    AuthorityInput(
        name="fixed evidence staff admission",
        owner="auth.staff_permission_admission",
        kind=AuthorityKind.CONTROL_INPUT,
        source="Named support/project/task/completion preview purposes freshly revalidated before/after waits; flush-only participants accept only active support.ticket_lifecycle or operations.field_completion inbound executor context.",
    ),
)
CONCERNS = (
    "protected shared complaint restoration and issued scope",
    "exact outage replacement frozen audience coverage",
    "incident before ticket completion graph locks",
)
SERVICES = (
    SOTService(
        name="network.outage_completion_evidence",
        module="app.services.topology.outage_completion",
        owns=CONCERNS,
        depends_on=(
            "operations.work_order_commands",
            "auth.staff_permission_admission",
        ),
        notes="Real resolver of typed current shared scope and graph locks. Original records retain their canonical owners. Active named outer-owner checks are inbound participant authority, not outbound mutation calls. No restoration, issuance, customer closure, notification or financial consequences.",
        contract=ServiceContract(
            concerns=tuple(
                ConcernContract(
                    name=name,
                    role=OwnerRole.RESOLVER,
                    input_names=tuple(x.name for x in INPUTS),
                )
                for name in CONCERNS
            ),
            authoritative_inputs=INPUTS,
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Protected typed reads and flush-free graph preparation in the admitted caller transaction; no executor or record mutation.",
                locking="Discover all roots; sorted Incident then all Ticket linkage fences/Tickets, globally sorted selected Subscriptions, caller Project/task then WOs/heads. Re-read immutable link identities after waits; unexpected incident aborts entire transaction.",
                idempotency="Read-only interpretation; caller pins immutable native receipt and exact scope snapshot.",
                retries="Whole transaction retry on graph drift; no new Incident acquired under Ticket.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(
                    "network.outage_completion_evidence." + code
                    for code in (
                        "denied",
                        "participant_required",
                        "shared_scope_changed",
                        "shared_scope_unavailable",
                        "issuance_provenance_unavailable",
                        "shared_scope_proof_unavailable",
                        "shared_scope_binding_changed",
                        "shared_incident_not_restored",
                        "replacement_scope_mismatch",
                        "replacement_audience_incomplete",
                    )
                ),
                mapping_owner="calling lifecycle owner",
                fail_closed_on=(
                    "unknown historical native service proof",
                    "omitted actual issuance",
                    "stale restoration or service binding",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.outage_completion_evidence",
                verification="Migration681 strict exact empty adoption and immutable canonical lineage guards; no historical inference/backfill.",
            ),
            steward="network and field operations",
            design_refs=("docs/designs/SHARED_OUTAGE_COMPLETION.md",),
            test_refs=(
                "tests/test_shared_outage_completion.py",
                "tests/architecture/test_shared_outage_completion_boundary.py",
                "tests/integration/test_shared_outage_completion_acceptance.py",
            ),
        ),
    ),
)
