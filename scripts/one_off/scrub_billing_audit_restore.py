"""Retired: a full restored database cannot be made portable by a denylist.

The earlier implementation rewrote known credential and identity columns in a
full production restore. That boundary was unsound: unlisted tables containing
communications, support conversations, recipients, metadata or a newly added
PII column could survive unchanged.

Do not replace this tombstone with more scrub patterns. Run the billing audit
beside the isolated restore on the explicitly approved trusted host. Only the
positive-allowlisted evidence written by ``billing_alignment_audit.py`` may
leave that boundary. Destroy the restore when the controlled audit is complete.

This module deliberately imports no database library and opens no connection.
It remains at the old path so stale operator commands fail closed.
"""

from __future__ import annotations

import sys

RETIREMENT_MESSAGE = (
    "scrub_billing_audit_restore is retired: keep the full restore inside the "
    "trusted isolated host and export only allowlisted billing audit evidence"
)


def main() -> int:
    print(RETIREMENT_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
