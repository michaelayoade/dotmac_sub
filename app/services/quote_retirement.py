"""Deployment retirement authority for CRM-backed quote work, never native quotes."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class QuoteRetirementOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["retired"] = "retired"
    reconciled: Literal[0] = 0
    refreshed: Literal[False] = False
    actions_available: Literal[False] = False


def retirement_outcome() -> QuoteRetirementOutcome:
    """CRM retirement is an approved deployment contract, not a binding fallback."""
    return QuoteRetirementOutcome()
