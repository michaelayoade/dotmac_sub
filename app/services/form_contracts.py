"""Editor/form page contracts — the ``ui.form_contracts`` owner.

The UI information/action standard requires every editor to describe a
transition, not a bag of fields: show current and proposed state, surface
prerequisites near the affected control, preview impact before high-impact
changes, and name irreversible consequences. This module owns the declarative
vocabulary for that contract; the *owning domain service* evaluates the
prerequisites and computes the impact — templates render the evaluated state
and never re-derive eligibility.

Pilot consumer: the customer plan-change editor
(``customer_portal_flow_changes.PLAN_CHANGE_FORM``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FormPrerequisite:
    """One named precondition, evaluated by the command owner."""

    key: str
    label: str
    met: bool
    reason: str | None = None  # shown near the control when unmet


@dataclass(frozen=True)
class FormConsequence:
    """One named effect of submitting — rendered before the primary action."""

    key: str
    label: str


@dataclass(frozen=True)
class FormContract:
    """Declarative editor contract for one form surface.

    ``command_owner`` names the service that executes the transition and
    re-checks every prerequisite at execution time — the rendered contract is
    disclosure, never enforcement.
    """

    key: str
    title: str
    entity: str
    command_owner: str
    consequences: tuple[FormConsequence, ...] = ()

    def state(self, prerequisites: list[FormPrerequisite]) -> dict:
        """Renderable contract state for the template."""
        return {
            "key": self.key,
            "title": self.title,
            "entity": self.entity,
            "prerequisites": prerequisites,
            "unmet_prerequisites": [p for p in prerequisites if not p.met],
            "consequences": self.consequences,
            "submittable": all(p.met for p in prerequisites),
        }


# ---------------------------------------------------------------------------
# Registry and governance
#
# FORM_CONTRACT_REGISTRY is the executable registry of declared editor
# contracts (declare with ``register(...)`` next to the command owner).
# HIGH_IMPACT_EDITORS is the checked-in governance list: every editor whose
# submit moves money, changes service, or is irreversible must either carry a
# registered contract or a conscious waiver. The architecture test
# (tests/architecture/test_form_contracts_governance.py) enforces both. This
# list is configuration — extend it in review, never bypass it in code.
# ---------------------------------------------------------------------------

FORM_CONTRACT_REGISTRY: dict[str, FormContract] = {}


def register(contract: FormContract) -> FormContract:
    """Register a declared editor contract (one owner per key)."""
    if contract.key in FORM_CONTRACT_REGISTRY:
        raise ValueError(f"form contract {contract.key!r} already registered")
    FORM_CONTRACT_REGISTRY[contract.key] = contract
    return contract


@dataclass(frozen=True)
class HighImpactEditor:
    """Governance entry for one high-impact editor surface.

    status: "contracted" (contract_key must be registered), "pending"
    (adoption owed — the governance test reports these but does not fail), or
    "waived" (a conscious decision not to contract; reason required).
    """

    key: str
    surface: str
    status: str
    contract_key: str | None = None
    reason: str | None = None


SUPPORT_RESOLUTION_FORM = register(
    FormContract(
        key="support.request_resolution_confirmation",
        title="Request customer resolution confirmation",
        entity="support_ticket",
        command_owner="support.ticket_lifecycle",
        consequences=(
            FormConsequence(
                "pinned_basis",
                "Pins the exact current accepted completion evidence for all enrolled linked work orders.",
            ),
            FormConsequence(
                "customer_confirmation",
                "Moves the ticket to pending confirmation and schedules its grace timer; customer confirmation or dispute remains separate.",
            ),
            FormConsequence(
                "replace_capability",
                "A replacement request invalidates the previous customer confirmation capability; exact retries retain the original deadline.",
            ),
        ),
    )
)


HIGH_IMPACT_EDITORS: tuple[HighImpactEditor, ...] = (
    HighImpactEditor(
        key="support.request_resolution_confirmation",
        surface="admin support ticket customer confirmation request",
        status="contracted",
        contract_key="support.request_resolution_confirmation",
    ),
    HighImpactEditor(
        key="field_completion.enroll",
        surface="admin completion enrollment",
        status="contracted",
        contract_key="field_completion.enroll",
    ),
    HighImpactEditor(
        key="field_completion.publish_criteria",
        surface="admin native completion criteria",
        status="contracted",
        contract_key="field_completion.publish_criteria",
    ),
    HighImpactEditor(
        key="field_completion.publish_policy",
        surface="admin completion publish_policy",
        status="contracted",
        contract_key="field_completion.publish_policy",
    ),
    HighImpactEditor(
        key="field_completion.decide",
        surface="admin completion decide",
        status="contracted",
        contract_key="field_completion.decide",
    ),
    HighImpactEditor(
        key="field_completion.revoke",
        surface="admin completion revoke",
        status="contracted",
        contract_key="field_completion.revoke",
    ),
    HighImpactEditor(
        key="project_engineering.policy",
        surface="admin Project engineering policy",
        status="contracted",
        contract_key="project_engineering.policy",
    ),
    HighImpactEditor(
        key="project_engineering.draft",
        surface="admin Project engineering draft",
        status="contracted",
        contract_key="project_engineering.draft",
    ),
    HighImpactEditor(
        key="project_engineering.review",
        surface="admin Project engineering review",
        status="contracted",
        contract_key="project_engineering.review",
    ),
    HighImpactEditor(
        key="project_engineering.revoke",
        surface="admin Project engineering revoke",
        status="contracted",
        contract_key="project_engineering.revoke",
    ),
    HighImpactEditor(
        key="project_engineering.issue",
        surface="admin Project engineering issue",
        status="contracted",
        contract_key="project_engineering.issue",
    ),
    HighImpactEditor(
        key="admin.subscription_test_connection",
        surface="admin customer subscription Test Connection",
        status="contracted",
        contract_key="admin.subscription_test_connection",
    ),
    HighImpactEditor(
        key="admin.billing_mode_transition",
        surface="/admin/billing/accounts/{account_id}/billing-mode",
        status="contracted",
        contract_key="admin.billing_mode_transition",
    ),
    HighImpactEditor(
        key="admin.subscription_service_invoice",
        surface="/admin/billing/invoices/subscription-service",
        status="contracted",
        contract_key="admin.subscription_service_invoice",
    ),
    HighImpactEditor(
        key="customer.plan_change",
        surface="/portal/services/{id}/change",
        status="contracted",
        contract_key="customer.plan_change",
    ),
    HighImpactEditor(
        key="admin.subscription_suspend_cancel",
        surface="admin catalog subscription suspend/cancel actions",
        status="pending",
    ),
    HighImpactEditor(
        key="admin.subscription_correction",
        surface="admin catalog mistaken-subscription correction",
        status="contracted",
        contract_key="admin.subscription_correction",
    ),
    HighImpactEditor(
        key="admin.invoice_void",
        surface="admin billing invoice void",
        status="pending",
    ),
    HighImpactEditor(
        key="admin.prepaid_draft_reconciliation",
        surface="admin billing prepaid draft reconciliation confirmation",
        status="contracted",
        contract_key="admin.prepaid_draft_reconciliation",
    ),
    HighImpactEditor(
        key="admin.customer_subledger_opening_correction",
        surface="admin billing customer-subledger opening correction",
        status="contracted",
        contract_key="admin.customer_subledger_opening_correction",
    ),
    HighImpactEditor(
        key="admin.credit_note_create",
        surface="admin billing credit-note issue",
        status="pending",
    ),
    HighImpactEditor(
        key="admin.payment_refund_reversal",
        surface="admin billing refund/reversal",
        status="pending",
    ),
    HighImpactEditor(
        key="admin.dunning_action",
        surface="admin billing dunning case actions",
        status="pending",
    ),
    HighImpactEditor(
        key="admin.rbac_role_edit",
        surface="admin system role/permission editor",
        status="pending",
    ),
    HighImpactEditor(
        key="admin.device_delete_replace",
        surface="admin network device delete/replace",
        status="pending",
    ),
    HighImpactEditor(
        key="reseller.financial_ops",
        surface="reseller billing/settlement actions",
        status="pending",
    ),
)
