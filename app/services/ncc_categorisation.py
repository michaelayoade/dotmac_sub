"""NCC complaint categorisation — the rule, applied on save, stored on the ticket.

The NCC quarterly complaints return (①) files a Category and a sub-category
code per complaint. CRM derived both at *report* time by keyword-matching free
text, storing nothing: nobody could correct a mis-classification, and the
filed numbers silently changed whenever the keyword rules changed.

Here the rule runs when a ticket is written (``support.Tickets`` — the declared
lifecycle owner) and the outcome is **stored on the ticket**:

* ``ncc_category`` / ``ncc_subcategory`` — what gets filed.
* ``ncc_category_source`` / ``ncc_subcategory_source`` — ``derived`` (we
  guessed from text) or ``agent`` (a human said so).

An ``agent`` value is never re-derived. The report reads the stored fields, so
a filing is a projection of captured decisions rather than a fresh guess.

The keyword rules are ported verbatim from CRM's ``_ncc_category_value`` /
``_ncc_subcategory_value`` so the first backfill reproduces what CRM would
have filed; from then on corrections accumulate. Note the rules only ever
produce 7 of the 18 NCC categories — everything unmatched lands in the
"Quality of Service (Data)" catch-all. That is CRM's behaviour, preserved
deliberately: the fix is agent correction, not a cleverer guess.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.ncc_workbook import (
    SUBCATEGORY_BY_CODE,
    category_code_value,
    clean_category,
    clean_subcategory_code,
)

SOURCE_DERIVED = "derived"
SOURCE_AGENT = "agent"
VALID_SOURCES = frozenset({SOURCE_DERIVED, SOURCE_AGENT})


def _normalised_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _searchable(ticket_type: object, subject: object, description: object) -> str:
    return " ".join(
        part
        for part in (
            _normalised_key(ticket_type),
            _normalised_key(subject),
            _normalised_key(description),
        )
        if part
    )


def derive_category(
    ticket_type: object, *, subject: object = "", description: object = ""
) -> str:
    """The NCC Category for a complaint, from its type/subject/description.

    Ordered first-match-wins over one concatenated search string, so a keyword
    in the description can outrank the ticket type. Ported verbatim from CRM.
    """
    searchable = _searchable(ticket_type, subject, description)
    if any(
        term in searchable
        for term in ("failed payment", "payment failed", "payment transaction")
    ):
        return "Failed Payment Transactions"
    if any(
        term in searchable
        for term in ("billing", "invoice", "charged", "charge", "balance", "refund")
    ):
        return "Billing"
    if any(
        term in searchable
        for term in ("call down", "call center", "customer care", "support")
    ):
        return "Call Center / Customer Care"
    if any(term in searchable for term in ("bts", "base station", "basestation")):
        return "BTS Issues"
    if any(
        term in searchable
        for term in (
            "router replacement",
            "faulty terminal",
            "cpe",
            "ont",
            "onu",
            "terminal",
        )
    ):
        return "Faulty Terminals"
    if "data depletion" in searchable:
        return "Data Depletion"
    return "Quality of Service (Data)"


def _subcategory_dropdown_value(issue_code: str) -> str:
    row = SUBCATEGORY_BY_CODE.get(issue_code)
    return f"{row['issue_code']} - {row['name']}" if row else ""


def derive_subcategory(
    category: object,
    ticket_type: object,
    *,
    subject: object = "",
    description: object = "",
) -> str:
    """The NCC sub-category code, which is only valid under its own Category.

    Mostly resolves to each category's "…50" catch-all — only Quality of
    Service (Data) and a couple of others discriminate. Ported verbatim.
    """
    cleaned_category = clean_category(category)
    searchable = _searchable(ticket_type, subject, description)
    if cleaned_category == "Billing":
        return _subcategory_dropdown_value("A50")
    if cleaned_category == "Call Center / Customer Care":
        return _subcategory_dropdown_value("B50")
    if cleaned_category == "Quality of Service (Data)":
        if any(term in searchable for term in ("slow", "speed", "bandwidth")):
            return _subcategory_dropdown_value("D3")
        if any(
            term in searchable for term in ("outage", "disconnection", "no internet")
        ):
            return _subcategory_dropdown_value("D2")
        if any(
            term in searchable
            for term in ("troubleshooting", "intermittent", "authentication")
        ):
            return _subcategory_dropdown_value("D1")
        return _subcategory_dropdown_value("D4")
    if cleaned_category == "Faulty Terminals":
        return _subcategory_dropdown_value("F1" if "router" in searchable else "F50")
    if cleaned_category == "BTS Issues":
        return _subcategory_dropdown_value(
            "G1" if ("bts" in searchable or "base station" in searchable) else "G50"
        )
    if cleaned_category == "Data Depletion":
        return _subcategory_dropdown_value("Q1")
    if cleaned_category == "Failed Payment Transactions":
        return _subcategory_dropdown_value("R1")
    code = category_code_value(cleaned_category)
    return _subcategory_dropdown_value(f"{code}50") if code else ""


def derive_for(
    *, ticket_type: object, subject: object, description: object
) -> tuple[str, str]:
    """(category, subcategory) for the given text. Pure — no ticket, no session."""
    category = derive_category(ticket_type, subject=subject, description=description)
    subcategory = derive_subcategory(
        category, ticket_type, subject=subject, description=description
    )
    return category, subcategory


def apply_to_ticket(
    ticket: Any, *, explicit_data: dict[str, Any] | None = None
) -> None:
    """Store the NCC classification on a ticket being written.

    Called by ``support.Tickets`` on create/update — the declared ticket
    lifecycle owner. Never a parallel writer.

    An agent-supplied value wins and is marked ``agent``; it is never
    re-derived afterwards, even if the ticket's text later changes. A derived
    value is refreshed on every write so it tracks edits to the text it came
    from.

    A category and its sub-category are stored independently: an agent may
    correct one without the other. If a later category change leaves an
    agent-set sub-category invalid under it, the value is kept and the
    workbook's own cross-consistency check flags the row at export
    (``[FAIL] …``). Surfacing the conflict to the compliance officer beats
    silently overwriting what a human chose.
    """
    explicit = explicit_data or {}

    if explicit.get("ncc_category"):
        ticket.ncc_category = clean_category(explicit["ncc_category"])
        ticket.ncc_category_source = SOURCE_AGENT
    elif ticket.ncc_category_source != SOURCE_AGENT:
        ticket.ncc_category = derive_category(
            ticket.ticket_type, subject=ticket.title, description=ticket.description
        )
        ticket.ncc_category_source = SOURCE_DERIVED

    if explicit.get("ncc_subcategory"):
        ticket.ncc_subcategory = clean_subcategory_code(
            explicit["ncc_subcategory"], category=ticket.ncc_category
        )
        ticket.ncc_subcategory_source = SOURCE_AGENT
    elif ticket.ncc_subcategory_source != SOURCE_AGENT:
        ticket.ncc_subcategory = derive_subcategory(
            ticket.ncc_category,
            ticket.ticket_type,
            subject=ticket.title,
            description=ticket.description,
        )
        ticket.ncc_subcategory_source = SOURCE_DERIVED
