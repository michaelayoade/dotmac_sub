"""Who owns a collections consequence here, and what collections may not write.

Ledger rows `COL-R5` and `COL-R7` in the Starter repository's
`docs/inventories/commercial-retirement-ledger.md`. This module holds the
declarations their ratchet reads, so the boundary is reviewable in one place
rather than inferred from a grep pattern spread across a test.

## The two layers, and why there are two

ADR-0030's collections design: *"A collections request is not permission or a
service-state write. The service owner revalidates and permits/refuses/applies
the transition."*

So a dunning or prepaid decision produces a typed COMMAND, and two other owners
take it from there:

* ``app.services.account_lifecycle`` **decides** — it takes the row locks,
  revalidates the credential against the state the preview promised, and
  permits or refuses.
* ``app.services.radius_access_state`` **applies** — it performs the profile
  column writes, including the ``pre_throttle_radius_profile_id`` remember and
  restore semantics.

Collections holds neither half. It still READS both columns to build its
preview (``collections/_core.py``'s ``preview_financial_access_*``), because a
decision input is not a write; the ratchet is on assignment, not on reference.

## A correction to the ledger row

`COL-R5` names ``account_lifecycle`` as the consequence owner and states its
ratchet as *"assignments to ``radius_profile_id``/``pre_throttle_radius_profile_id``
outside ``account_lifecycle``"*. Measured on ``origin/dev`` at ``ad3b32152``,
that predicate is too broad to be useful: there were eighteen assignment sites,
and only eight of them were collections'. The other ten belong to catalog,
RADIUS provisioning, PPPoE auto-provisioning and fair-use-policy session
enforcement — none of which is COL-R5's writer, and none of which is migrating
here. ``account_lifecycle`` itself wrote neither column, so the literal
predicate would have counted every one of those ten as debt on day one.

The predicate below therefore names the WRITER by location instead of naming
the column across the tree. ``OUT_OF_PROGRAMME_CREDENTIAL_WRITERS`` records the
rest with their real owners, so that a reader can tell "not collections' debt"
from "nobody looked".
"""

from __future__ import annotations

from typing import Final

# ── The authority ────────────────────────────────────────────────────────────

#: The tree that may no longer assign a RADIUS profile column.
COLLECTIONS_WRITER_ROOT: Final[str] = "app/services/collections"

#: Revalidates and permits or refuses a requested credential consequence.
CONSEQUENCE_DECIDER: Final[str] = "app.services.account_lifecycle"

#: Performs the profile column writes the decider authorised.
CONSEQUENCE_APPLIER: Final[str] = "app.services.radius_access_state"

#: The two columns whose assignment the ratchet counts.
CREDENTIAL_PROFILE_COLUMNS: Final[tuple[str, ...]] = (
    "radius_profile_id",
    "pre_throttle_radius_profile_id",
)

#: Zero, and it stays zero. Collections requests; it does not write.
COLLECTIONS_CREDENTIAL_WRITE_SITES: Final[int] = 0


# ── COL-R7: the retired dead writers ─────────────────────────────────────────

#: Symbols deleted by COL-R7. They had no production caller — only tests that
#: existed to exercise them. The names are kept because a guard that stops
#: mattering the moment it passes stops mattering exactly when a regression
#: would become invisible.
RETIRED_DEAD_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        "_throttle_account",
        "_restore_throttle",
        "resolve_cases_for_account",
        "enforcement_health_blocked",
    }
)


# ── Not collections' debt, and not unexamined either ─────────────────────────

#: Every other module that assigns a RADIUS profile column, with the owner it
#: belongs to. These are outside the commercial retirement programme: each is a
#: provisioning, catalog or session-enforcement decision, not a collections
#: consequence. Recorded rather than ignored, so the ratchet's silence about
#: them is a stated position instead of an oversight.
OUT_OF_PROGRAMME_CREDENTIAL_WRITERS: Final[dict[str, str]] = {
    "app/services/access_credential_binding.py": "provisioning: credential rebind",
    "app/services/catalog/subscriptions.py": "catalog: offer plan-change alignment",
    "app/services/enforcement.py": "session enforcement: fair-use-policy throttle",
    "app/services/pppoe_credentials.py": "provisioning: PPPoE auto-provisioning",
    "app/services/radius.py": "RADIUS provisioning: RadiusUser synchronisation",
    "app/services/radius_access_state.py": CONSEQUENCE_APPLIER,
    "app/services/web_catalog_subscriptions.py": "catalog: admin credential edit",
}

__all__ = [
    "COLLECTIONS_CREDENTIAL_WRITE_SITES",
    "COLLECTIONS_WRITER_ROOT",
    "CONSEQUENCE_APPLIER",
    "CONSEQUENCE_DECIDER",
    "CREDENTIAL_PROFILE_COLUMNS",
    "OUT_OF_PROGRAMME_CREDENTIAL_WRITERS",
    "RETIRED_DEAD_SYMBOLS",
]
