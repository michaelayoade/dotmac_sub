"""The NCC classification rule: derived on save, stored, agent-correctable.

Ported verbatim from CRM's keyword rules so the first backfill reproduces what
CRM would have filed. The rules only ever produce 7 of the 18 NCC categories —
that is preserved deliberately; the fix is agent correction, not a cleverer
guess.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import ncc_categorisation as cat


def _ticket(**overrides) -> SimpleNamespace:
    return SimpleNamespace(
        ticket_type=overrides.pop("ticket_type", None),
        title=overrides.pop("title", ""),
        description=overrides.pop("description", ""),
        ncc_category=overrides.pop("ncc_category", None),
        ncc_category_source=overrides.pop("ncc_category_source", None),
        ncc_subcategory=overrides.pop("ncc_subcategory", None),
        ncc_subcategory_source=overrides.pop("ncc_subcategory_source", None),
        **overrides,
    )


def test_category_rules_match_crm():
    assert cat.derive_category("", subject="Payment failed at checkout") == (
        "Failed Payment Transactions"
    )
    assert cat.derive_category("", subject="Wrong invoice") == "Billing"
    assert cat.derive_category("", subject="Customer care never called") == (
        "Call Center / Customer Care"
    )
    assert cat.derive_category("", subject="BTS down in area") == "BTS Issues"
    assert cat.derive_category("", subject="ONT keeps blinking") == "Faulty Terminals"
    assert cat.derive_category("", subject="Data depletion too fast") == (
        "Data Depletion"
    )


def test_unmatched_text_falls_back_to_the_qos_catch_all():
    """CRM's behaviour, kept: only 7 of 18 categories are reachable by rule."""
    assert cat.derive_category("", subject="Something else entirely") == (
        "Quality of Service (Data)"
    )


def test_description_can_outrank_ticket_type():
    """All three fields are concatenated into one search string."""
    assert (
        cat.derive_category(
            "general", subject="hello", description="my invoice is wrong"
        )
        == "Billing"
    )


def test_subcategory_discriminates_within_qos_data():
    assert cat.derive_subcategory(
        "Quality of Service (Data)", "", subject="speeds are slow"
    ).startswith("D3 - ")
    assert cat.derive_subcategory(
        "Quality of Service (Data)", "", subject="total outage"
    ).startswith("D2 - ")


def test_apply_stores_derived_values():
    ticket = _ticket(title="Wrong invoice amount")
    cat.apply_to_ticket(ticket, explicit_data={})
    assert ticket.ncc_category == "Billing"
    assert ticket.ncc_category_source == cat.SOURCE_DERIVED
    assert ticket.ncc_subcategory.startswith("A50 - ")
    assert ticket.ncc_subcategory_source == cat.SOURCE_DERIVED


def test_apply_marks_an_explicit_value_as_agent_owned():
    ticket = _ticket(title="Wrong invoice amount")
    cat.apply_to_ticket(ticket, explicit_data={"ncc_category": "BTS Issues"})
    assert ticket.ncc_category == "BTS Issues"
    assert ticket.ncc_category_source == cat.SOURCE_AGENT


def test_apply_never_re_derives_an_agent_value():
    ticket = _ticket(
        title="Wrong invoice amount",
        ncc_category="BTS Issues",
        ncc_category_source=cat.SOURCE_AGENT,
    )
    cat.apply_to_ticket(ticket, explicit_data={})
    assert ticket.ncc_category == "BTS Issues"
    assert ticket.ncc_category_source == cat.SOURCE_AGENT


def test_apply_refreshes_a_derived_value_when_text_changes():
    ticket = _ticket(
        title="Wrong invoice amount",
        ncc_category="Quality of Service (Data)",
        ncc_category_source=cat.SOURCE_DERIVED,
    )
    cat.apply_to_ticket(ticket, explicit_data={})
    assert ticket.ncc_category == "Billing"


def test_an_unknown_explicit_category_is_rejected_not_stored():
    """A junk value must not reach a regulatory filing."""
    ticket = _ticket(title="Wrong invoice amount")
    cat.apply_to_ticket(ticket, explicit_data={"ncc_category": "Not A Real Category"})
    assert ticket.ncc_category == ""
    assert ticket.ncc_category_source == cat.SOURCE_AGENT
