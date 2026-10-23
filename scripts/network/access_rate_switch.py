"""Operator adapter for the governed single-rate-owner switch.

Adapter for ``access.rate_profile_switch``. It owns no decision: it forwards to
the owner commands, which re-run the preflight under a lock, flip the setting
with an audit record, project, read the rates back and switch off again on any
mismatch.

    # report only (default): preflight divergences, nothing is changed
    python -m scripts.network.access_rate_switch on --reason "CHG-123"

    # make the change
    python -m scripts.network.access_rate_switch on --reason "CHG-123" --apply

    # roll back
    python -m scripts.network.access_rate_switch off --reason "CHG-123" --apply

Exit codes: 0 success or dry-run; 1 refused or failed (domain error, or an
automatic switch-off after a failed read-back); 2 invalid arguments.
"""

from __future__ import annotations

import sys

from app.services.access_rate_switch import main

if __name__ == "__main__":
    sys.exit(main())
