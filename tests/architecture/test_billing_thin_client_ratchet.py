"""Thin-client ratchet on Sub-owned billing surface (ADR 0020).

Sub is becoming a thin client of the build-once Starter `dotmac-billing`
package set. Until the ledger switch completes, legacy billing code stays
frozen in place; after adoption begins it must only shrink. This ratchet
makes both directions deliberate:

- the billing file inventory is exact — a new billing module in Sub is new
  billing semantics in the wrong repository and fails here until Michael
  records an exception and the baseline edit is reviewed;
- the total line count is two-directional — shrink the surface and lower
  the baseline in the same change, so gains lock in;
- transitional cutover tools (opening census, numbering proofs) may exist
  only while registered in ``billing_transitional_tools.json`` with a
  deletion trigger, and may not grow past their registered size.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.architecture import billing_thin_client_inventory as inv

ROOT = Path(__file__).resolve().parents[2]


def _parse_baseline(text: str) -> tuple[int, tuple[str, ...]]:
    total: int | None = None
    files: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("TOTAL "):
            total = int(line.split()[1])
        else:
            files.append(line)
    assert total is not None, "baseline is missing its TOTAL line"
    return total, tuple(files)


def test_billing_surface_inventory_and_total_match_baseline() -> None:
    baseline_text = (ROOT / inv.BASELINE_PATH).read_text(encoding="utf-8")
    pinned_total, pinned_files = _parse_baseline(baseline_text)
    current_files = inv.billing_surface_files(ROOT)
    current_total = inv.total_billing_lines(ROOT)

    new_files = sorted(set(current_files) - set(pinned_files))
    removed_files = sorted(set(pinned_files) - set(current_files))
    assert not new_files, (
        "New billing modules appeared in Sub. Billing semantics belong in "
        "Starter dotmac-billing (thin-client gate); "
        "adding surface here requires Michael's approval and a reviewed baseline edit:\n  "
        + "\n  ".join(new_files)
    )
    assert not removed_files, (
        "Billing modules were deleted but the ratchet baseline was not "
        "lowered in the same change; regenerate it so the shrinkage locks "
        "in:\n  " + "\n  ".join(removed_files)
    )
    assert current_total == pinned_total, (
        f"Billing surface is {current_total} lines, baseline pins "
        f"{pinned_total}. Shrinkage must lower the baseline in the same "
        "change; net growth requires Michael's explicit approval."
    )


def test_money_writer_surface_outside_billing_paths_matches_baseline() -> None:
    """Writers hiding behind non-billing filenames are pinned too.

    The path-shaped surface above cannot see modules like topup intents or
    prepaid funding reconciliation; this sweep detects direct money-model
    instantiation anywhere under ``app/`` outside that surface, so the
    writer inventory the migration must retire stays honest.
    """

    baseline_text = (ROOT / inv.MONEY_WRITER_BASELINE_PATH).read_text(encoding="utf-8")
    pinned_total, pinned_files = _parse_baseline(baseline_text)
    current_files = inv.money_writer_files_outside_surface(ROOT)
    current_total = inv.total_money_writer_lines(ROOT)

    new_files = sorted(set(current_files) - set(pinned_files))
    removed_files = sorted(set(pinned_files) - set(current_files))
    assert not new_files, (
        "New money-writer modules appeared outside the billing surface. "
        "Money-state writes belong in Starter dotmac-billing/payments "
        "(thin-client gate); a new writer requires "
        "Michael's approval and a "
        "reviewed baseline edit:\n  " + "\n  ".join(new_files)
    )
    assert not removed_files, (
        "Money-writer modules were retired but the baseline was not lowered "
        "in the same change; regenerate it so the shrinkage locks in:\n  "
        + "\n  ".join(removed_files)
    )
    assert current_total == pinned_total, (
        f"Money-writer surface is {current_total} lines, baseline pins "
        f"{pinned_total}. Shrinkage must lower the baseline in the same "
        "change; net growth requires Michael's explicit approval."
    )


def test_money_writer_baseline_rendering_is_normalized() -> None:
    pinned = (ROOT / inv.MONEY_WRITER_BASELINE_PATH).read_text(encoding="utf-8")
    assert pinned == inv.render_money_writer_baseline(ROOT), (
        "money_writer_surface_baseline.txt is not the normalized rendering — "
        "regenerate it from billing_thin_client_inventory"
        ".render_money_writer_baseline instead of editing by hand"
    )


def test_baseline_rendering_is_normalized() -> None:
    """The committed baseline is exactly `render_baseline` output."""

    pinned = (ROOT / inv.BASELINE_PATH).read_text(encoding="utf-8")
    assert pinned == inv.render_baseline(ROOT), (
        "billing_thin_client_baseline.txt is not the normalized rendering — "
        "regenerate it from billing_thin_client_inventory.render_baseline "
        "instead of editing by hand"
    )


def _registry() -> list[dict[str, object]]:
    data = json.loads((ROOT / inv.TRANSITIONAL_REGISTRY_PATH).read_text())
    tools = data["tools"]
    assert isinstance(tools, list)
    return tools


def test_every_transitional_tool_is_registered_with_deletion_trigger() -> None:
    detected = set(inv.transitional_tool_files(ROOT))
    registered = {str(entry["path"]) for entry in _registry()}
    unregistered = sorted(detected - registered)
    assert not unregistered, (
        "Transitional cutover tools exist without a registry entry. "
        "Register each with a deletion trigger in "
        "billing_transitional_tools.json (thin-client gate):\n  "
        + "\n  ".join(unregistered)
    )
    stale = sorted(registered - detected)
    assert not stale, (
        "Registered transitional tools no longer exist on disk; remove "
        "their registry entries in the same change:\n  " + "\n  ".join(stale)
    )


def test_transitional_tools_cannot_grow() -> None:
    for entry in _registry():
        path = str(entry["path"])
        assert entry.get("purpose"), f"{path}: registry entry needs a purpose"
        trigger = entry.get("deletion_trigger")
        assert trigger, f"{path}: registry entry needs a deletion trigger"
        assert entry.get("owner"), f"{path}: registry entry needs an owner"
        current = inv.line_count(ROOT, path)
        baseline = int(entry["baseline_lines"])  # type: ignore[arg-type]
        assert current == baseline, (
            f"{path} is {current} lines, registry pins {baseline}. "
            "Transitional tools must not grow; if this tool shrank, lower "
            "baseline_lines in the same change so the gain locks in."
        )
