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
