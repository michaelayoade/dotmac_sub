"""Guard: templates must not compute business totals over loop rows.

Summing money or usage across the rows of a paginated list in Jinja produces a
figure that is wrong past the first page, and it duplicates a decision the
owning read/context service must make (see the customer billing KPI regression,
where Total/Outstanding/Overdue were summed over the paginated invoice page).
The presentation layer projects values the backend computes; it never derives
them.

Detection targets the Jinja namespace accumulation pattern —
``{% set ns.field = ns.field + <row value> %}`` — which is the only way to carry
a running total across a ``{% for %}`` loop (a plain ``{% set %}`` resets each
iteration). Adding ``+ 1`` (a counter) or ``+ [x]`` (building a list) is
legitimate presentation bookkeeping and is not flagged.

The baseline is migration debt and may only shrink. Resolve a template by moving
the total into its read owner and removing its line from the baseline file.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
BASELINE = Path(__file__).with_name("template_business_arithmetic_baseline.txt")

# {% set NS.FIELD = NS.FIELD (+|-) REST %} with NS.FIELD referring to itself —
# the running-total accumulation across a loop.
_ACCUMULATE = re.compile(
    r"\{%-?\s*set\s+([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*"
    r"\1\.\2\s*[+\-]\s*(?P<added>.+?)-?%\}",
    re.DOTALL,
)


def _added_term_is_business_value(added: str) -> bool:
    """True when the accumulated term is a domain value, not a counter or a
    list build. ``+ 1`` (counter) and ``+ [col.key]`` (list build) are ordinary
    presentation bookkeeping; ``+ record.amount`` is a business total."""
    term = added.strip().lstrip("(").strip()
    if term.startswith("["):
        return False
    if re.fullmatch(r"\d+(\.\d+)?\s*\)*", term):
        return False
    return True


def _offending_templates() -> set[str]:
    offenders: set[str] = set()
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for match in _ACCUMULATE.finditer(text):
            if _added_term_is_business_value(match.group("added")):
                offenders.add(path.relative_to(PROJECT_ROOT).as_posix())
                break
    return offenders


def _baseline() -> set[str]:
    if not BASELINE.exists():
        return set()
    return {
        line.strip()
        for line in BASELINE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_no_new_template_business_total_accumulation() -> None:
    new = _offending_templates() - _baseline()
    assert not new, (
        "Templates computing a business total by accumulating a domain value "
        "over loop rows (wrong past page one; the total belongs in the read "
        "owner): " + ", ".join(sorted(new))
    )


def test_template_arithmetic_baseline_has_no_stale_entries() -> None:
    stale = _baseline() - _offending_templates()
    assert not stale, (
        "Baseline lists templates that no longer accumulate a business total; "
        "remove them so the guard stays honest: " + ", ".join(sorted(stale))
    )
