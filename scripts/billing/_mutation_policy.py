"""Billing mutation policy guard (decision 2026-10-12: no more mutations).

Billing repair writes are disabled pending the ADR 0007 obligation-identity
migration. Guarded scripts call ``refuse_apply`` straight after argument
parsing, before any database session is opened.
"""

from __future__ import annotations

import sys

REFUSAL_MESSAGE = (
    "billing repair writes are disabled by policy (2026-10-12); "
    "record the case for the billing exceptions review instead."
)


def refuse_apply(apply: bool) -> None:
    """Exit non-zero with a stderr message when ``--apply`` was requested."""
    if apply:
        print(REFUSAL_MESSAGE, file=sys.stderr)
        raise SystemExit(1)
