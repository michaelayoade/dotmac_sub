"""The one seam between Sub and the two composed inbox distributions.

Sub imports `dotmac_inbox*` HERE and nowhere else. That is not style: it is what
makes "the module is the owner" checkable — a grep for the distribution outside
this package is a second writer, and
`tests/architecture/test_inbox_module_boundary.py` fails on one.

- `references` — Sub identifiers to module references, and Sub's four presence
  states to the module's three.
- `conversations` — `dotmac_inbox`: threads, messages, read cursors.
- `operations` — `dotmac_inbox_operations`: queues, routing, presence,
  admission, assignment, dispatch.

Importing this package registers Sub's channel declarations as a side effect,
because `dotmac_inbox.threading` cannot answer anything until they exist. That
import is load-bearing, not tidying.

See `docs/adr/0013-inbox-authority-cutover.md`.
"""

from __future__ import annotations

from app.services import inbox_channels as _inbox_channels  # noqa: F401
from app.services.inbox_module import conversations, operations, references

__all__ = ["conversations", "operations", "references"]
