"""Typed Automation Center support for temporary connection-test requests."""

from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationActionInput,
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
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

ACTIONS = (
    AutomationActionCapability(
        key="billing.test_connection.notify_finance",
        label="Notify Finance of repeated Test Connections",
        entity_type="access.test_connection",
        command_owner="financial.test_connection_finance_review",
        command_name="notify_test_connection_finance",
        input_schema_version=1,
        inputs=(
            AutomationActionInput(
                key="service_team_id",
                label="Finance team (in-app and email)",
                value_type=AutomationValueType.uuid,
            ),
        ),
        author_permission="notification:write",
        runtime_scope="automation:runtime",
        idempotency="event/rule-version/step with immutable team and recipient snapshot",
        runtime_enabled=True,
    ),
)

_OWNER = "financial.test_connection_finance_review"
_CONCERN = "temporary Test Connection Finance review notifications"
SERVICES = (
    SOTService(
        name=_OWNER,
        module="app.services.test_connection_finance",
        owns=(_CONCERN, "Test Connection Finance recipient snapshots"),
        depends_on=(
            "access.test_connection",
            "automation.rule_definitions",
            "events.store",
            "customer.accounts",
            "operations.service_team_lifecycle",
            "communications.staff_notifications",
            "observability.audit_log",
        ),
        contract=ServiceContract(
            events=EventContract(
                event_types=("billing.test_connection.finance_review_queued",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Version 1 contains review/source-event/customer IDs, recipient count and rule provenance; no recipient contacts.",
                replay="One deterministic evidence event per committed Finance review receipt; exact replay emits nothing.",
            ),
            concerns=(
                ConcernContract(
                    name=_CONCERN,
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "native creation evidence",
                        "frozen Test Connection event",
                        "canonical customer identity",
                        "configured team and active staff",
                        "typed staff notification participant",
                        "published Finance workflow version",
                    ),
                ),
                ConcernContract(
                    name="Test Connection Finance recipient snapshots",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "frozen Test Connection event",
                        "configured team and active staff",
                    ),
                    canonical_writer=_OWNER,
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="published Finance workflow version",
                    owner="automation.rule_definitions",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="immutable published version, exact action position and configured Finance team UUID",
                ),
                AuthorityInput(
                    name="native creation evidence",
                    owner="access.test_connection",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="native test_connection_grants subscriber and subscription identity and creation interval",
                ),
                AuthorityInput(
                    name="frozen Test Connection event",
                    owner="events.store",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="version-1 event with creation-time count and bounded request references",
                ),
                AuthorityInput(
                    name="canonical customer identity",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscriber display identity and account number",
                ),
                AuthorityInput(
                    name="configured team and active staff",
                    owner="operations.service_team_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="published workflow team UUID and active team-to-SystemUser membership",
                ),
                AuthorityInput(
                    name="typed staff notification participant",
                    owner="communications.staff_notifications",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="source-linked in-app/email rows and per-recipient dedupe keys",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary="One execute_owner_command atomically stages the recipient snapshot, participant notifications, and audit. Participants flush only.",
                locking="Lock the durable source event before receipt lookup or any channel staging; unique event/version/step arbitrates replay.",
                idempotency="Deterministic review UUID pins team, evidence digest, and recipient IDs. Replay returns the original audience without new sends.",
                retries="Retry after full rollback; committed receipts survive failures between action completion and automation step acknowledgement.",
            ),
            errors=ErrorContract(
                retryable_codes=(f"{_OWNER}.recipients_unavailable",),
                domain_codes=tuple(
                    f"{_OWNER}.{code}"
                    for code in (
                        "invalid_scope",
                        "invalid_evidence",
                        "recipients_unavailable",
                        "replay_conflict",
                    )
                )
                + owner_command_boundary_error_codes(_OWNER),
                mapping_owner="automation.execution",
                fail_closed_on=(
                    "wrong tenant or target",
                    "missing native source grant",
                    "missing recipients or email",
                    "changed replay evidence",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner=_OWNER,
                verification="threshold, time window, customer isolation, notification replay, and migrated PostgreSQL tests",
                cutover_gate="Native Test Connection migration 645 precedes Finance receipt migration 646; operators explicitly publish the workflow after trigger/action deployment.",
                fallback_retirement="No keyword matching, scheduler workaround, direct delivery, or automatic workflow publication.",
            ),
            steward="billing and Finance operations",
            design_refs=(
                "docs/designs/TEST_CONNECTION_FINANCE_ALERT.md",
                "docs/designs/AUTOMATION_CENTER_SOT.md",
            ),
            test_refs=(
                "tests/test_test_connection_finance.py",
                "tests/integration/test_test_connection_finance.py",
                "tests/architecture/test_test_connection_boundary.py",
            ),
        ),
    ),
)
