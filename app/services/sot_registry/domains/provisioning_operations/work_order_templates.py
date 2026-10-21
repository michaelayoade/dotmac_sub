"""Read-only work purpose, technology and scope-recommendation owner."""

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
from app.services.work_order_templates import TemplateErrorCode

_OWNER = "operations.work_order_templates"
_INPUTS = (
    AuthorityInput(
        name="explicit work purpose and access technology",
        owner=_OWNER,
        kind=AuthorityKind.CONTROL_INPUT,
        source="Immutable operator-selected classification and native WorkOrderKind; no inference from legacy work_type, title, tags or metadata.",
    ),
    AuthorityInput(
        name="approved field-scope recommendation catalog",
        owner=_OWNER,
        kind=AuthorityKind.CONTROL_INPUT,
        source="Versioned checked-in recommendation requirements in app.services.work_order_templates; engineering owners supply site-specific acceptance criteria.",
    ),
)
_CONCERNS = (
    "explicit work purpose and technology compatibility",
    "approved field-scope template recommendations",
)

SERVICES = (
    SOTService(
        name=_OWNER,
        module="app.services.work_order_templates",
        owns=_CONCERNS,
        notes="Planning recommendations only. No evidence, dispatch, completion, activation, provisioning, billing or ticket closure authority.",
        contract=ServiceContract(
            concerns=tuple(
                ConcernContract(
                    name=concern,
                    role=OwnerRole.POLICY,
                    input_names=tuple(item.name for item in _INPUTS),
                )
                for concern in _CONCERNS
            ),
            authoritative_inputs=_INPUTS,
            transaction=TransactionContract(
                mode=TransactionMode.NOT_APPLICABLE,
                boundary="Pure immutable query; no database, transaction or external side effects.",
                locking="Not applicable: checked-in immutable catalog revision.",
                idempotency="Identical typed inputs at one catalog version return identical recommendations.",
                retries="No retries for classification errors; rerun query after explicit corrected selection.",
            ),
            errors=ErrorContract(
                domain_codes=tuple(code.value for code in TemplateErrorCode),
                mapping_owner="Calling work-order command/query adapter",
                fail_closed_on=(
                    "untyped classification",
                    "incompatible customer/infrastructure purpose",
                    "unknown deployment/rollout/upgrade/relocation technology",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner=_OWNER,
                verification="Pure catalog compatibility and technology-specific recommendation tests.",
                cutover_gate="Explicit opt-in only; existing freeform work_type and unclassified legacy rows retain their semantics.",
                fallback_retirement="No inference or legacy fallback introduced.",
            ),
            steward="field operations",
            design_refs=("docs/designs/WORK_ORDER_TEMPLATE_CATALOG.md",),
            test_refs=(
                "tests/test_work_order_templates.py",
                "tests/architecture/test_work_order_template_ownership.py",
            ),
        ),
    ),
)
