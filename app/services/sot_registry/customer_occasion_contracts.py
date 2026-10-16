"""Executable ownership declaration for annual customer greetings."""

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

CUSTOMER_OCCASION_SERVICE = SOTService(
    name="communications.customer_occasions",
    module="app.services.customer_occasions",
    owns=(
        "annual customer greeting configuration and admission",
        "annual customer greeting review",
        "annual customer greeting delivery preflight",
    ),
    depends_on=("communications.intents", "communications.customer_policy"),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                "annual customer greeting configuration and admission",
                OwnerRole.COMMAND_WRITER,
                ("configuration", "customer dates and status", "delivery evidence"),
                canonical_writer="communications.customer_occasions",
            ),
            ConcernContract(
                "annual customer greeting review",
                OwnerRole.RESOLVER,
                ("configuration", "customer dates and status", "delivery evidence"),
            ),
            ConcernContract(
                "annual customer greeting delivery preflight",
                OwnerRole.POLICY,
                ("configuration", "customer dates and status", "delivery evidence"),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                "configuration",
                "communications.customer_occasions",
                AuthorityKind.AUTHORITATIVE_RECORD,
                "customer_occasion_automations; migration seeds both disabled",
            ),
            AuthorityInput(
                "customer dates and status",
                "customer.accounts",
                AuthorityKind.AUTHORITATIVE_RECORD,
                "Subscriber.date_of_birth/account_start_date/status/email/marketing_opt_in",
            ),
            AuthorityInput(
                "delivery evidence",
                "communications.intents",
                AuthorityKind.AUTHORITATIVE_RECORD,
                "communication_intents and notifications; annual dedupe key",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.OWNER_MANAGED,
            boundary="Each save or bounded process command enters execute_owner_command once; intent participants flush only.",
            locking="Lock occasion configuration before customer UUID order; skip busy configuration. Saves lock and compare revision.",
            idempotency="One intent key per occasion/customer/calendar year, including suppressed outcomes. Same-day drainage excludes recorded intents.",
            retries="Retry complete rolled-back batches; input errors fail closed. Delivery retries belong to notification transport.",
        ),
        errors=ErrorContract(
            domain_codes=(
                "communications.customer_occasions.invalid_command",
                "communications.customer_occasions.not_configured",
                "communications.customer_occasions.stale_revision",
                *owner_command_boundary_error_codes(
                    "communications.customer_occasions"
                ),
            ),
            mapping_owner="Admin occasion routes and Celery occasion task",
            fail_closed_on=(
                "missing dates",
                "invalid templates/timezones",
                "stale configuration",
                "missing marketing consent",
            ),
        ),
        events=EventContract(
            event_types=("customer_occasion.changed",),
            schema_version=1,
            delivery_owner="events.dispatcher",
            compatibility="Version 1 records occasion, revision, and processed count without identity or message content.",
            replay="Record-only audit evidence; never replay transport from configuration audit. Annual intents remain delivery authority.",
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE,
            new_owner="communications.customer_occasions",
        ),
        steward="customer communications",
        design_refs=("docs/designs/CUSTOMER_OCCASION_AUTOMATIONS.md",),
        test_refs=(
            "tests/test_customer_occasions.py",
            "tests/test_customer_occasion_values.py",
            "tests/integration/test_customer_occasion_automations.py",
            "tests/architecture/test_customer_occasion_ownership.py",
        ),
    ),
)
