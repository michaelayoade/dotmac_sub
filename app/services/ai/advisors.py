"""Advisors: AI advises ON an owned report projection, it never re-derives one.

``docs/designs/AI_SOT.md`` calls personas "the resolver" that builds context
from the owning domain's read models. Taken literally — as CRM did — each
persona queries raw models itself, which is a **parallel derivation path**
sitting next to the projection the domain owner already computes. CRM needed
a ``data_quality`` scorer per persona precisely because each re-derived its
own context and then had to grade it.

Sub already owns ~35 report projections (revenue, churn, network,
technician, ticket-SLA, NCC, MRR). An advisor therefore declares the
projection it advises on via ``report_key``; the CALLER fetches that report
from its owner and hands the dict to the engine. The engine never touches a
domain model, so the boundary in
``tests/architecture/test_ai_boundaries.py`` holds by construction rather
than by vigilance — and the quality gate disappears, because a report the
owner computed does not need grading by us.

``OutputField``/``OutputSchema`` are ported from CRM's
``personas/_base.py``: a provider-agnostic JSON contract worth keeping.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutputField:
    name: str
    type: str
    description: str
    required: bool = True


@dataclass(frozen=True)
class OutputSchema:
    """The JSON shape we ask the model for, rendered into the prompt."""

    fields: tuple[OutputField, ...]

    def required_keys(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def to_instruction(self) -> str:
        lines = ["Return a JSON object with these keys:"]
        for f in self.fields:
            req = "required" if f.required else "optional"
            lines.append(f'  - "{f.name}" ({f.type}, {req}): {f.description}')
        return "\n".join(lines)


@dataclass(frozen=True)
class AdvisorSpec:
    """One advisor, bound to one owned report projection.

    ``report_key`` names the projection this advises on. It is documentation
    with teeth: the binding is explicit, so a reader can find the owner that
    computes the input, and a caller cannot quietly feed an advisor something
    it was never designed to read.
    """

    key: str
    name: str
    domain: str
    description: str
    report_key: str
    system_prompt: str  # must contain the {output_instructions} slot
    output_schema: OutputSchema
    default_max_tokens: int = 1200
    default_endpoint: str = "primary"  # primary|secondary
    setting_key: str | None = None
    insight_ttl_hours: int = 72
    severity_classifier: Callable[[dict[str, Any]], str] | None = None


class AdvisorRegistry:
    def __init__(self) -> None:
        self._advisors: dict[str, AdvisorSpec] = {}

    def register(self, spec: AdvisorSpec) -> None:
        if spec.key in self._advisors:
            logger.warning("Advisor %s already registered, overwriting", spec.key)
        self._advisors[spec.key] = spec

    def get(self, key: str) -> AdvisorSpec:
        spec = self._advisors.get(key)
        if not spec:
            raise ValueError(f"Unknown advisor: {key}")
        return spec

    def list_all(self) -> list[AdvisorSpec]:
        return list(self._advisors.values())

    def keys(self) -> list[str]:
        return list(self._advisors.keys())


advisor_registry = AdvisorRegistry()


# ── ticket SLA advisor ──────────────────────────────────────────────────────
# Advises on `ticket_sla_reports.summary(db, start_at, end_at)` — the owned
# projection behind /admin/reports (app/web/admin/reports.py). Its shape:
#   total_clocks, total_breaches, breach_rate,
#   by_status / by_service_team / by_assignee:
#       [{key, label?, total, breached, breach_rate}]
# The prompt describes only those fields; nothing here invents any.

TICKET_SLA_REPORT_KEY = "ticket_sla_reports.summary"


def _sla_severity(parsed: dict[str, Any]) -> str:
    """Severity from the model's own risk read, clamped to our vocabulary."""
    value = str(parsed.get("risk_level") or "").strip().lower()
    return {
        "critical": "critical",
        "high": "warning",
        "medium": "suggestion",
        "low": "info",
    }.get(value, "info")


TICKET_SLA_ADVISOR = AdvisorSpec(
    key="ticket_sla_advisor",
    name="Ticket SLA Advisor",
    domain="tickets",
    description=(
        "Reads the owned ticket-SLA summary and points out where breaches "
        "concentrate and what to look at first."
    ),
    report_key=TICKET_SLA_REPORT_KEY,
    setting_key="intelligence_ticket_sla_advisor_enabled",
    system_prompt=(
        "You are an ISP support operations analyst. You are given a ticket "
        "SLA summary computed by the operator's own reporting system. Fields:\n"
        "  total_clocks: SLA clocks in the window\n"
        "  total_breaches: how many breached\n"
        "  breach_rate: breaches / clocks (0..1)\n"
        # Braces doubled: the engine renders this through str.format() to fill
        # {output_instructions}, so a literal brace must be escaped or format()
        # reads it as a field and raises KeyError.
        "  by_status, by_service_team, by_assignee: buckets of "
        "{{key, label, total, breached, breach_rate}}\n\n"
        "Explain where breaches concentrate and what to investigate first. "
        "Cite only numbers present in the report — do not estimate, "
        "extrapolate, or invent causes. If the report is empty or too small "
        "to read into, say so plainly rather than speculating.\n\n"
        "{output_instructions}"
    ),
    output_schema=OutputSchema(
        fields=(
            OutputField(
                name="title",
                type="string",
                description="One line naming the main SLA finding.",
            ),
            OutputField(
                name="summary",
                type="string",
                description=("A short paragraph citing the report's own figures."),
            ),
            OutputField(
                name="risk_level",
                type="string",
                description="One of: low, medium, high, critical.",
            ),
            OutputField(
                name="recommended_actions",
                type="array of strings",
                description="Concrete next checks for a support lead.",
                required=False,
            ),
        )
    ),
    severity_classifier=_sla_severity,
)

advisor_registry.register(TICKET_SLA_ADVISOR)
