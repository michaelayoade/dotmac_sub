"""Paging-off is per-model policy: no hard-coded ``screen-length`` in app/.

``screen-length 0 temporary`` is Huawei switch syntax that MA5608T/MA5800 OLTs
reject, so every site goes through ``OltSshPolicy.paging_off_command``
(``olt_ssh._apply_paging_off``). Docs: tests/transcripts/README.md.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
# Filtering such lines OUT of a replayed backup is safe: it is never sent.
ALLOWED = {"app/services/network/olt_ssh_config.py"}


def test_no_screen_length_string_literal_in_app() -> None:
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP.parent).as_posix()
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "screen-length" in node.value
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"hard-coded paging command: {offenders}"
