"""Native temporary subscription test-access ownership."""

from app.services.automation_contracts import (
    AutomationConditionField,
    AutomationOperator,
    AutomationTriggerCapability,
    AutomationValueType,
)
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

TRIGGERS = (
    AutomationTriggerCapability(
        key="billing.test_connection.created",
        label="Test Connection created",
        event_type="billing.test_connection.created",
        event_schema_version=1,
        entity_type="access.test_connection",
        tenant_id_field="tenant_id",
        entity_id_field="grant_id",
        fields=(
            AutomationConditionField(
                key="customer_id",
                label="Customer",
                value_type=AutomationValueType.uuid,
                operators=(AutomationOperator.in_values,),
            ),
            AutomationConditionField(
                key="count_7d",
                label="Test Connections created in the preceding 7 days",
                value_type=AutomationValueType.integer,
                operators=(
                    AutomationOperator.greater_than,
                    AutomationOperator.greater_than_or_equal,
                    AutomationOperator.equals,
                    AutomationOperator.less_than_or_equal,
                ),
            ),
        ),
        author_permission="subscription:test_connection",
        runtime_enabled=True,
    ),
)

OWNER = "access.test_connection"
CONCERN = "bounded subscription test-access grants"
SERVICE = SOTService(
    name=OWNER,
    module="app.services.test_connection",
    owns=(
        CONCERN,
        "current time-bounded test-access evidence",
        "current test-access network consequence",
        "customer-scoped Test Connection creation counts",
    ),
    depends_on=(
        "control.settings_spec",
        "runtime.durable_timers",
        "events.dispatcher",
        "observability.audit_log",
        "access.radius_target_registry",
    ),
    contract=ServiceContract(
        concerns=(
            ConcernContract(
                name="customer-scoped Test Connection creation counts",
                role=OwnerRole.RESOLVER,
                input_names=("grant records",),
            ),
            ConcernContract(
                name=CONCERN,
                role=OwnerRole.AUTHORITATIVE_RECORD,
                input_names=("staff command", "grant records", "test configuration"),
                canonical_writer=OWNER,
            ),
            ConcernContract(
                name="current time-bounded test-access evidence",
                role=OwnerRole.RESOLVER,
                input_names=("grant records", "security evidence"),
            ),
            ConcernContract(
                name="current test-access network consequence",
                role=OwnerRole.TRANSPORT,
                input_names=("grant records", "security evidence"),
            ),
        ),
        authoritative_inputs=(
            AuthorityInput(
                name="staff command",
                owner="auth.permission_gate",
                kind=AuthorityKind.CONTROL_INPUT,
                source="permission-gated, authenticated staff command context and customer/subscription scope",
            ),
            AuthorityInput(
                name="grant records",
                owner=OWNER,
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="test_connection_grants rows and absolute UTC intervals",
            ),
            AuthorityInput(
                name="test configuration",
                owner="control.settings_spec",
                kind=AuthorityKind.CONTROL_INPUT,
                source="declared system-wide radius test duration and safety bound plus verified deadline capability",
            ),
            AuthorityInput(
                name="security evidence",
                owner="access.subscription_lifecycle",
                kind=AuthorityKind.AUTHORITATIVE_RECORD,
                source="active explicit fraud locks; financial locks are never admission inputs",
            ),
        ),
        transaction=TransactionContract(
            mode=TransactionMode.OWNER_MANAGED,
            boundary="Each public write enters execute_owner_command once; audit, event and required timer are flush-only participants.",
            locking="Subscription FOR UPDATE serializes activation; account-key advisory lock serializes creation counts across subscriptions before timestamp selection. Grant FOR UPDATE serializes expiry/delivery; partial unique index permits one open grant.",
            idempotency="command_id uniquely identifies an activation; repeat commands return its unchanged interval and mismatched replays fail closed.",
            retries="At-least-once events recompute current network state; stale expiry cannot expire a newer grant; transport retries never extend the interval.",
        ),
        errors=ErrorContract(
            domain_codes=owner_command_boundary_error_codes(OWNER)
            + tuple(
                f"{OWNER}.{suffix}"
                for suffix in (
                    "permission_denied",
                    "subscription_not_found",
                    "grant_not_found",
                    "invalid_duration",
                    "deadline_not_verified",
                    "already_active",
                    "idempotency_conflict",
                    "network_not_ready",
                    "ambiguous_login",
                    "security_hold",
                    "not_due",
                )
            ),
            mapping_owner="permission-gated customer web adapters and test-connection event adapter",
            fail_closed_on=(
                "unverified network deadline",
                "live shared login without subscription ownership",
                "explicit fraud hold",
                "missing usable provisioning",
                "out-of-scope subscription",
            ),
        ),
        events=EventContract(
            event_types=(
                "subscription.test_connection_changed",
                "billing.test_connection.created",
            ),
            schema_version=1,
            delivery_owner="events.dispatcher",
            compatibility="Existing changed-event v1 remains unchanged. Creation-event v1 freezes customer-specific seven-day count and bounded references with command provenance in the activation transaction.",
            replay="Consequence always resolves current access; expiry consumes the exact grant identity, never a saved commercial-state snapshot.",
        ),
        projections=(
            ProjectionContract(
                name="temporary network access",
                input_names=("grant records", "security evidence"),
                writer="access.radius_projection",
                freshness="Absolute deadline is checked by application readers and RADIUS SQL at authentication; NAS Session-Timeout bounds existing sessions.",
                stale_behavior="An expired override is not selected, even while its raw rows remain; normal rows remain maintained throughout the test.",
                drift_signal="Exact keyed fingerprint includes normal rows, prefixed override rows, deadline and grant identity.",
                rebuild_operation="radius_population.reconcile_usernames and reconcile_test_connection_network",
                repair_owner="access.radius_projection",
            ),
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.NATIVE, new_owner=OWNER
        ),
        steward="customer experience and network operations",
        design_refs=(
            "docs/designs/SUBSCRIPTION_TEST_CONNECTION.md",
            "docs/designs/TEST_CONNECTION_FINANCE_ALERT.md",
        ),
        test_refs=(
            "tests/test_subscription_test_connection.py",
            "tests/integration/test_subscription_test_connection.py",
            "tests/architecture/test_subscription_test_connection_boundary.py",
        ),
    ),
)
