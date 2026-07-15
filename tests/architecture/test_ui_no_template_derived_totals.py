"""A headline total must come from the backend, never be summed in a template.

Part of enforcement item 3 of docs/UI_INFORMATION_AND_ACTION_STANDARD.md: the
backend owns truth; a template renders it. Computing a business total inside
Jinja lets the template become a parallel decision-maker, and (for a paginated
list) produces a total that silently disagrees with its own rows -- e.g. the
customer "Total Usage" that changed when you paged, because it summed only the
loaded page.

Detection is deliberately narrow to keep false positives near zero: a Jinja
`{% set X = X + ... %}` that reassigns a variable to itself-plus-something, i.e.
a running accumulator. That is the "build a total across rows in the template"
smell; it is not tripped by ordinary `{% set x = value %}` binding.

This is a RATCHET, mirroring tests/architecture/test_huawei_control_plane_writes.py:
pre-existing occurrences are grandfathered in BACKLOG and the list may only
SHRINK. Two assertions therefore hold the line from both sides:
  * no template OUTSIDE the backlog may introduce the pattern (no new debt);
  * every backlog entry must STILL match (a fixed template is forced off the
    list, so the debt can never silently masquerade as paid down).

To pay a template off: remove the accumulator (have the owning read/context
service return the total, with provenance) and delete the entry here. The
customer-facing entries are the priority -- their totals are shown above
paginated tables and are user-visible. Single-entity sums (e.g. one invoice's
line items on its own detail page) are lower risk but are still tracked here so
the ban is uniform.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"

# {% set foo = foo + ... %} / {% set foo.bar = foo.bar + ... %}  (self-accumulator)
_ACCUMULATOR = re.compile(
    r"\{%-?\s*set\s+([A-Za-z_][\w.]*)\s*=\s*([A-Za-z_][\w.]*)\s*\+"
)

# Templates that computed a total in Jinja before this guard existed.
# This list may only shrink. Do not add to it -- return the total from the
# owning backend service instead. Customer-facing entries are marked; fix those
# first (they render above paginated tables).
BACKLOG = {
    "templates/customer/usage/_content.html",  # customer-facing: "Total Usage" over the page
    "templates/customer/billing/index.html",  # customer-facing
    "templates/customer/services/change_plan.html",  # customer-facing
    "templates/admin/design_system/index.html",  # design-system demo
    "templates/components/data/data_grid.html",  # shared component helper
    "templates/components/ui/macros.html",  # shared component helper
}


def _templates_with_accumulator() -> set[str]:
    found: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in _ACCUMULATOR.finditer(text):
            if match.group(1) == match.group(2):
                found.add(path.relative_to(ROOT).as_posix())
                break
    return found


def test_no_new_template_derived_totals() -> None:
    offenders = _templates_with_accumulator()
    new = sorted(offenders - BACKLOG)
    assert not new, (
        "These templates compute a headline total in Jinja instead of rendering "
        "one from the backend (see docs/UI_INFORMATION_AND_ACTION_STANDARD.md). "
        "Return the total from the owning read/context service:\n  " + "\n  ".join(new)
    )


def test_backlog_only_shrinks() -> None:
    offenders = _templates_with_accumulator()
    fixed = sorted(BACKLOG - offenders)
    assert not fixed, (
        "These templates no longer compute a total in Jinja -- remove them from "
        "BACKLOG in this test so the ratchet stays tight:\n  " + "\n  ".join(fixed)
    )
