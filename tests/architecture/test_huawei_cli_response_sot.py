"""Huawei response semantics must remain owned by one classifier."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TARGETS = {
    ROOT / "app/services/network/parsers/cli.py",
    ROOT / "app/services/network/olt_ssh_session.py",
    ROOT / "app/services/network/olt_protocol_adapters.py",
    ROOT / "app/services/network/ont_inventory.py",
    ROOT / "app/services/network/ont_authorization.py",
    ROOT / "app/services/network/olt_config_pack_live_audit.py",
    ROOT / "app/services/web_network_ont_actions/config_setters.py",
}
TARGETS.update((ROOT / "app/services/network").glob("olt_ssh*.py"))
TARGETS.update((ROOT / "app/services/network/olt_ssh_ont").glob("*.py"))

RESPONSE_MARKERS = (
    "already exists",
    "does not exist",
    "insufficient privilege",
    "is not exist",
    "ont is not online",
    "parameter error",
    "unknown command",
)


def _local_response_comparisons(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        values = [node.left, *node.comparators]
        for value in values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            lowered = value.value.casefold()
            if any(marker in lowered for marker in RESPONSE_MARKERS):
                offenders.append((node.lineno, value.value))
    return offenders


def test_huawei_cli_response_text_has_one_owner() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): matches
        for path in sorted(TARGETS)
        if (matches := _local_response_comparisons(path))
    }
    assert not offenders, (
        "Huawei CLI response text must be classified by "
        "app.services.network.huawei_cli_response; local string comparisons "
        f"create firmware-dependent drift: {offenders}"
    )


def test_legacy_entry_points_delegate_to_classifier() -> None:
    parser_source = (ROOT / "app/services/network/parsers/cli.py").read_text(
        encoding="utf-8"
    )
    session_source = (ROOT / "app/services/network/olt_ssh_session.py").read_text(
        encoding="utf-8"
    )

    assert "has_huawei_cli_error" in parser_source
    assert "classify_huawei_cli_response" in session_source
    assert "HUAWEI_ERROR_PATTERNS" not in parser_source
    assert "_ERROR_PATTERNS" not in session_source
