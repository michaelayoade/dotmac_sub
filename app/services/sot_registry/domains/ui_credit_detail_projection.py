"""Read-only credit document presentation; financial authority stays unchanged."""

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="ui_credit_detail_projection",
    services=(
        SOTService(
            name="ui.credit_detail_projection",
            module="app.services.web_billing_credits",
            owns=("read-only credit-note detail projection",),
            depends_on=("financial.credit_notes", "ui.status_presentation"),
            notes=(
                "CreditDetailQuery selects one document through CreditNotes.get; "
                "CreditDetailView freezes its facts for the read-only admin page. "
                "No lifecycle, monetary calculation, persistence, or package "
                "authority transfers from financial.credit_notes."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="read-only credit-note detail projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical credit-note document",
                            "credit status presentation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical credit-note document",
                        owner="financial.credit_notes",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="CreditNotes.get: credit identity, account, money, dates, memo, lines and application evidence",
                    ),
                    AuthorityInput(
                        name="credit status presentation",
                        owner="ui.status_presentation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="credit_note_status_presentation for the authoritative credit-note status",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="The adapter-owned Session reads through CreditNotes.get; projection never flushes, commits or writes.",
                    locking="No mutation and no lock escalation.",
                    idempotency="Repeated queries project the current authoritative document without side effects.",
                    retries="A failed read can be repeated; no command or event is emitted.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="app.web.admin.billing_credits retains the incumbent credit owner's not-found response",
                    fail_closed_on=(
                        "missing credit document",
                        "missing billing:credit_note:read permission",
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="credit-note admin detail",
                        input_names=(
                            "canonical credit-note document",
                            "credit status presentation",
                        ),
                        writer="ui.credit_detail_projection",
                        freshness="Request-time query; no persisted or cached copy.",
                        stale_behavior="Reload reads current owner facts; application rows remain historical evidence rather than a derived balance.",
                        drift_signal="Projection tests compare exact owner amounts and evidence without recomputation.",
                        rebuild_operation="build_credit_detail(db, query=CreditDetailQuery(credit_note_id=...))",
                        repair_owner="ui.credit_detail_projection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="ui.credit_detail_projection",
                ),
                steward="billing UI operations",
                design_refs=(
                    "docs/FRONTEND_SPEC.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_web_billing_credit_detail.py",
                    "tests/architecture/test_credit_detail_projection_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.admin.billing_credits",
        "templates.admin.billing.credits",
        "templates.admin.billing.credit_detail",
    ),
    rule="Credit details project existing owner facts through a typed read-only query. Financial credit-note state and money remain exclusively owned by financial.credit_notes.",
)
