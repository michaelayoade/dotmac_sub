"""EventType pruning guard (cleanup M4).

Twenty event types with no emitter, consumer or test were removed. This pins the
removal and guards the failure mode that made it risky: the name-based emit
helpers do ``getattr(EventType, name, None)`` and silently drop an event whose
name no longer resolves. Every string literal handed to those helpers must
resolve to a real member.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.events.types import (
    AccountCreditApplicationState,
    AccountCreditFundingOrigin,
    EventType,
)

ROOT = Path(__file__).resolve().parents[2]

REMOVED_EVENT_VALUES = (
    "backdated_service_invoice.created",
    "backdated_service_invoice.reconstructed",
    "usage.recorded",
    "appointment.missed",
    "device.offline",
    "device.online",
    "session.started",
    "session.ended",
    "ont.signal_delta",
    "ont.ddm_alert",
    "ont.authorized",
    "ont.service_port_created",
    "ont.tr069_bound",
    "olt.circuit_opened",
    "olt.circuit_closed",
    "customer.login",
    "customer.logout",
    "customer.password_changed",
    "ticket.merged",
    "nas_backup.failed",
)

# Billing provenance vocabulary: not event types, must stay untouched.
BILLING_PROVENANCE_VALUES = {
    "verified_invoice_payment",
    "allocated",
    "no_allocatable_balance",
    "retained_as_account_credit",
}

# Kept on purpose: a retired producer whose durable historical events must
# still decode (see the comment in types.py).
RETAINED_FOR_HISTORY = ("service_team.party_cutover_adopted",)

NAME_BASED_EMIT_HELPERS = {
    "_emit_nas_event",
    "_emit_customer_event",
    "emit_customer_event",
    "_emit_reseller_event",
}
# Helpers that take the dotted event VALUE rather than the member name.
VALUE_BASED_EMIT_HELPERS = {"_emit_ont_event"}

# Pre-existing no-ops: these portal names have never been EventType members, so
# the getattr-based helper has always dropped them silently. Out of scope for a
# deletion-only cleanup; the set may only shrink.
KNOWN_UNRESOLVED_BASELINE = {
    "app/web/customer/routes.py: customer_service_reboot_requested",
    "app/web/customer/routes.py: customer_service_resumed",
    "app/web/customer/routes.py: customer_service_suspended",
}


def _members() -> tuple[set[str], set[str]]:
    return {item.name for item in EventType}, {item.value for item in EventType}


def test_pruned_event_types_are_gone() -> None:
    _, values = _members()
    for value in REMOVED_EVENT_VALUES:
        assert value not in values, value


def test_retained_and_provenance_vocabulary_is_untouched() -> None:
    _, values = _members()
    for value in RETAINED_FOR_HISTORY:
        assert value in values
    provenance = {item.value for item in AccountCreditFundingOrigin} | {
        item.value for item in AccountCreditApplicationState
    }
    assert BILLING_PROVENANCE_VALUES <= provenance


def test_no_source_references_a_pruned_event_value_or_member() -> None:
    members = {value.replace(".", "_") for value in REMOVED_EVENT_VALUES}
    offenders: list[str] = []
    for base in ("app", "templates", "scripts"):
        for path in (ROOT / base).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".html", ".js"}:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative == "app/services/events/types.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for value in REMOVED_EVENT_VALUES:
                if f'"{value}"' in text or f"'{value}'" in text:
                    offenders.append(f"{relative}: {value}")
            for member in members:
                if f"EventType.{member}" in text:
                    offenders.append(f"{relative}: EventType.{member}")
    assert offenders == []


def test_every_literal_event_name_handed_to_a_name_based_emitter_resolves() -> None:
    names, values = _members()
    unresolved: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            )
            literals: list[tuple[str, bool]] = []
            if called in NAME_BASED_EMIT_HELPERS | VALUE_BASED_EMIT_HELPERS:
                literals = [
                    (arg.value, called in NAME_BASED_EMIT_HELPERS)
                    for arg in node.args[1:2]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                ]
            elif (
                called == "EventType"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                literals = [(node.args[0].value, False)]
            for literal, is_member_name in literals:
                pool = names if is_member_name else values
                if literal not in pool:
                    relative = path.relative_to(ROOT).as_posix()
                    unresolved.append(f"{relative}: {literal}")
    assert set(unresolved) <= KNOWN_UNRESOLVED_BASELINE
