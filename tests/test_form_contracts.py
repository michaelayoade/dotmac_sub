"""Tests for the ui.form_contracts owner and the plan-change pilot contract."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.customer_portal_flow_changes import (
    PLAN_CHANGE_FORM,
    _plan_change_prerequisites,
)
from app.services.form_contracts import FormContract, FormPrerequisite


def test_contract_state_shape_and_submittability():
    contract = FormContract(key="t", title="T", entity="thing", command_owner="svc.cmd")
    ok = FormPrerequisite(key="a", label="A", met=True)
    bad = FormPrerequisite(key="b", label="B", met=False, reason="nope")
    state = contract.state([ok, bad])
    assert state["submittable"] is False
    assert [p.key for p in state["unmet_prerequisites"]] == ["b"]
    assert contract.state([ok])["submittable"] is True


def test_plan_change_prerequisites_mirror_the_command_gates():
    sub = SimpleNamespace(status=SimpleNamespace(value="active"))
    offers = [object()]

    met = _plan_change_prerequisites(sub, offers, Decimal("0.00"))
    assert all(p.met for p in met)

    # Arrears block (the submit command refuses on collection-blocking balance).
    arrears = _plan_change_prerequisites(sub, offers, Decimal("10.00"))
    unmet = {p.key: p for p in arrears if not p.met}
    assert set(unmet) == {"no_arrears"}
    assert "overdue" in unmet["no_arrears"].reason

    # Inactive subscription.
    inactive = _plan_change_prerequisites(
        SimpleNamespace(status=SimpleNamespace(value="suspended")),
        offers,
        Decimal("0.00"),
    )
    assert {p.key for p in inactive if not p.met} == {"subscription_active"}

    # No offers.
    no_offers = _plan_change_prerequisites(sub, [], Decimal("0.00"))
    assert {p.key for p in no_offers if not p.met} == {"offers_available"}


def test_plan_change_contract_names_the_command_owner_and_consequences():
    assert (
        PLAN_CHANGE_FORM.command_owner
        == "service_intent.subscription_lifecycle_execution"
    )
    keys = {c.key for c in PLAN_CHANGE_FORM.consequences}
    assert {"proration", "reprovision", "field_fulfillment"} <= keys
