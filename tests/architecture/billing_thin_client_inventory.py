"""Shared inventory for the billing thin-client ratchet.

Sub is migrating to a thin client of the build-once Starter platform
(`dotmac-billing`, ADR 0020). While that migration runs, Sub must not grow
new billing semantics of its own: billing rules are authored in Starter and
Sub supplies raw observations plus composition wiring only.

This module defines the billing surface whose size
``test_billing_thin_client_ratchet`` pins. The surface is deliberately
defined by path shape, not by judgement calls in review, so a new billing
module cannot slip in uncounted under a creative name inside these trees.
"""

from __future__ import annotations

from pathlib import Path

#: Path shapes that count as Sub-owned billing surface. `*billing*` service
#: modules are included alongside the package tree because billing logic in
#: this repository historically grew sideways into `web_billing_*` and other
#: top-level service modules; excluding them would understate the surface
#: the thin-client migration has to retire.
BILLING_SURFACE_GLOBS: tuple[str, ...] = (
    "app/services/billing/**/*.py",
    "app/services/billing_*.py",
    "app/services/web_billing_*.py",
    "app/services/*billing*.py",
    "scripts/migration/billing_*.py",
    "scripts/billing/**/*.py",
)

#: Transitional cutover tooling is detected by name so a new evidence/proof
#: tool cannot appear without being registered (with a deletion trigger) in
#: ``billing_transitional_tools.json``.
TRANSITIONAL_TOOL_GLOBS: tuple[str, ...] = (
    "app/services/billing/opening_*.py",
    "app/services/billing_numbering_*.py",
    "scripts/migration/billing_opening_*.py",
    "scripts/migration/billing_numbering_*.py",
)

BASELINE_PATH = Path("tests/architecture/billing_thin_client_baseline.txt")
TRANSITIONAL_REGISTRY_PATH = Path("tests/architecture/billing_transitional_tools.json")


def _collect(root: Path, globs: tuple[str, ...]) -> tuple[str, ...]:
    paths: set[str] = set()
    for pattern in globs:
        for path in root.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts:
                paths.add(path.relative_to(root).as_posix())
    return tuple(sorted(paths))


def billing_surface_files(root: Path) -> tuple[str, ...]:
    """Return every repository-relative billing-surface Python file."""

    return _collect(root, BILLING_SURFACE_GLOBS)


def transitional_tool_files(root: Path) -> tuple[str, ...]:
    """Return every detected transitional cutover tool file."""

    return _collect(root, TRANSITIONAL_TOOL_GLOBS)


def line_count(root: Path, relative_path: str) -> int:
    """Physical line count, matching how the baseline was generated."""

    return len((root / relative_path).read_text(encoding="utf-8").splitlines())


def total_billing_lines(root: Path) -> int:
    return sum(line_count(root, rel) for rel in billing_surface_files(root))


def render_baseline(root: Path) -> str:
    """Render the exact baseline text for the current tree.

    The ratchet compares this rendering with the committed baseline, so
    regenerating the baseline is: write this output to ``BASELINE_PATH`` in
    the same reviewed change that shrinks (or, by approved exception,
    grows) the surface.
    """

    files = billing_surface_files(root)
    total = sum(line_count(root, rel) for rel in files)
    lines = [
        "# Billing thin-client ratchet baseline (ADR 0020 migration).",
        "# Sub is becoming a thin client of Starter dotmac-billing: this",
        "# surface must net-shrink. The file inventory is exact and the",
        "# total is two-directional — shrink the code and lower this",
        "# baseline in the same change; growth requires Michael's explicit",
        "# exception recorded in docs/feature_freeze_exceptions.json.",
        "# Regenerate with tests.architecture.billing_thin_client_inventory",
        "# .render_baseline(<repo root>).",
        f"TOTAL {total}",
        *files,
    ]
    return "\n".join(lines) + "\n"
