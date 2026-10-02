"""Complete contracts for the bounded payment email authority cutover."""

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


def _service(
    *,
    name: str,
    module: str,
    concern: str,
    role: OwnerRole,
    mode: TransactionMode,
    inputs: tuple[AuthorityInput, ...],
    dependencies: tuple[str, ...],
    events: tuple[str, ...],
    errors: tuple[str, ...],
    test_refs: tuple[str, ...],
) -> SOTService:
    writer = role in {OwnerRole.COMMAND_WRITER, OwnerRole.AUTHORITATIVE_RECORD}
    return SOTService(
        name=name,
        module=module,
        owns=(concern,),
        depends_on=dependencies,
        notes="Implementation is dormant until explicit operator activation after actual-runtime RLS and content parity proof.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=concern,
                    role=role,
                    input_names=tuple(item.name for item in inputs),
                    canonical_writer=name if writer else None,
                ),
            ),
            authoritative_inputs=inputs,
            transaction=TransactionContract(
                mode=mode,
                boundary="Public activation/publication enters execute_owner_command once on a transaction-free session; event and delivery participants mutate and flush inside the caller transaction; rendering is read-only.",
                locking="Existing delivery is locked before episode, rendered parts and source decisions/coverage; first creators use a correlation advisory transaction lock; Studio publication uses its template row lock.",
                idempotency="Immutable source decision and tenant/payment/invoice/recipient identity plus unique coverage prevents a second physical queue row for the same accepted source; publication compares the expected version.",
                retries="Existing notification queue owns ETA wakeup, pending recovery, provider attempts and delivery outcomes; database conflicts retry the complete transaction. No second episode delivery engine exists.",
            ),
            errors=ErrorContract(
                domain_codes=(*owner_command_boundary_error_codes(name), *errors)
                if mode
                in {TransactionMode.OWNER_MANAGED, TransactionMode.COORDINATOR_MANAGED}
                else errors,
                mapping_owner="Staff/CSRF operator adapter or existing durable event/notification worker",
                fail_closed_on=(
                    "changed reviewed identity",
                    "unproved payment linkage",
                    "missing receipt number/URL",
                    "PostgreSQL SUPERUSER/BYPASSRLS runtime",
                    "HTML composition without a fragment contract",
                ),
            ),
            events=EventContract(
                event_types=events,
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Identifier-only cutover/publication/source evidence is persisted with dispatch_after_commit=False; invoice.paid retains the existing event envelope with additive typed payment causation.",
                replay="Exact source replay retains frozen content/coverage; identifier-only evidence never creates webhooks or customer delivery.",
            )
            if events
            else None,
            projections=(
                ProjectionContract(
                    name="physical payment email content",
                    input_names=tuple(item.name for item in inputs),
                    writer=name,
                    stale_behavior="Frozen source bodies remain authoritative; late uncovered sources execute individually; suppressed coverage is not credited.",
                    freshness="Queued atomically and recomposed only while pending before the first decision plus 60 seconds; quiet hours may set a later delivery time.",
                    drift_signal="Each rendered part has source event/recipient decision and immutable Studio template/version; delivery coverage is verified by communications.intents.",
                    rebuild_operation="prepare_claimed_payment_email retains full published bodies for eligible sources; receipt supplies primary subject.",
                    repair_owner="communications.payment_email_episodes",
                ),
            )
            if name == "communications.payment_email_episodes"
            else (),
            migration=MigrationContract(
                state=AuthorityMigrationState.INVENTORIED,
                old_owner="Legacy per-channel NotificationTemplate renderer/writer; independent receipt and invoice-paid emails",
                new_owner=name,
                verification="Receipt number/URL, both source outcomes, SMS preservation, late/HTML fallback, deadline, actual PostgreSQL RLS and concurrent claims.",
                cutover_gate="Explicit persisted activation after content adoption parity, exact-image staging proof and separately reviewed actual database role cutover.",
                fallback_retirement="Activated legacy email content writing is sealed; SMS remains its existing owner; late or incompatible emails execute the original individual published body through communications.intents.",
            ),
            steward="customer communications",
            design_refs=(
                "docs/designs/PAYMENT_EMAIL_COMPOSITION_CUTOVER.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=test_refs,
        ),
    )


_INPUTS = (
    AuthorityInput(
        name="published payment email content",
        owner="communications.payment_template_authoring",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Template Studio alone writes content; the named Sub publication coordinator binds its published service API to the operator tenant, reviewed identity and immutable version",
    ),
    AuthorityInput(
        name="proved payment invoice linkage",
        owner="financial.payments",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Succeeded payment, settled allocation/ledger and subscriber-scoped invoice; no temporal inference",
    ),
    AuthorityInput(
        name="canonical recipient decision and delivery",
        owner="communications.intents",
        kind=AuthorityKind.AUTHORITATIVE_RECORD,
        source="Persisted per-recipient accepted/suppressed planning, canonical timing and explicit source coverage",
    ),
    AuthorityInput(
        name="reviewed legacy policy identity",
        owner="communications.notification_service",
        kind=AuthorityKind.CONTROL_INPUT,
        source="Legacy UUID, conditions, purpose and channel policy remain product-owned; content writer is sealed on activation",
    ),
)
_DEPENDENCIES = (
    "communications.intents",
    "communications.notification_service",
    "financial.payments",
    "tenancy.operator_tenant",
)
_TESTS = (
    "tests/test_payment_email_queue_composition.py",
    "tests/test_payment_email_cutover_behavior.py",
    "tests/integration/test_payment_email_composition_pg.py",
    "tests/test_payment_template_adoption.py",
)

PAYMENT_EMAIL_SERVICES = (
    _service(
        name="communications.payment_email_cutover",
        module="app.services.payment_email_cutover",
        concern="reviewed payment email authority cutover",
        role=OwnerRole.COMMAND_WRITER,
        mode=TransactionMode.OWNER_MANAGED,
        inputs=_INPUTS,
        dependencies=_DEPENDENCIES,
        events=("payment_email_cutover.activated", "payment_email_composition.paused"),
        errors=(
            "payment_email_cutover.invalid_scope",
            "payment_email_cutover.invalid_identity",
            "payment_email_cutover.parity_failed",
            "payment_email_cutover.identity_changed",
            "payment_email_cutover.not_active",
            "payment_template_adoption.unsafe_runtime_role",
        ),
        test_refs=_TESTS,
    ),
    _service(
        name="communications.payment_email_episodes",
        module="app.services.payment_email_episodes",
        concern="payment email episode and rendered source evidence",
        role=OwnerRole.COMMAND_WRITER,
        mode=TransactionMode.PARTICIPANT,
        inputs=_INPUTS,
        dependencies=_DEPENDENCIES,
        events=("payment_email_source.collected",),
        errors=("payment_email_episodes.unsupported_database",),
        test_refs=_TESTS,
    ),
    _service(
        name="communications.payment_template_authoring",
        module="app.services.payment_template_authoring",
        concern="payment email Template Studio publication",
        role=OwnerRole.APPLICATION_COORDINATOR,
        mode=TransactionMode.COORDINATOR_MANAGED,
        inputs=_INPUTS,
        dependencies=_DEPENDENCIES,
        events=("payment_template.published",),
        errors=tuple(
            f"payment_template_authoring.{code}"
            for code in (
                "invalid_code",
                "invalid_tenant",
                "stale_version",
                "invalid_content",
                "missing_template",
            )
        ),
        test_refs=_TESTS,
    ),
    _service(
        name="communications.payment_email_content",
        module="app.services.payment_email_content",
        concern="published payment email rendering",
        role=OwnerRole.RESOLVER,
        mode=TransactionMode.READ_ONLY,
        inputs=_INPUTS,
        dependencies=_DEPENDENCIES,
        events=(),
        errors=tuple(
            f"payment_email_content.{code}"
            for code in (
                "identity_changed",
                "empty_content",
                "missing_receipt",
                "inactive",
            )
        ),
        test_refs=_TESTS,
    ),
    _service(
        name="communications.payment_invoice_paid",
        module="app.services.billing.payment_invoice_paid",
        concern="proved payment paid-invoice notification consequence",
        role=OwnerRole.COMMAND_WRITER,
        mode=TransactionMode.PARTICIPANT,
        inputs=_INPUTS,
        dependencies=_DEPENDENCIES,
        events=("invoice.paid",),
        errors=("payment_invoice_paid.invalid_scope",),
        test_refs=(
            "tests/test_payment_settlement_allocation_evidence.py",
            "tests/test_payment_email_cutover_behavior.py",
        ),
    ),
)
