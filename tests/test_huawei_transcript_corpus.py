"""Run every captured Huawei CLI transcript through its parser vs a golden.

Layout: tests/transcripts/huawei/<model>/<command>.txt with a sibling
<command>.expected.json. See tests/transcripts/README.md. A transcript with no
registered parser, or no golden, fails the test so captures cannot be added
silently. Set ``UPDATE_TRANSCRIPT_GOLDENS=1`` to (re)write goldens, then review
the diff.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.services.network.huawei_cli_response import (
    HuaweiCliResource,
    classify_huawei_cli_response,
    is_huawei_resource_absent,
)
from app.services.network.olt_ssh_ont.status import (
    normalize_inventory_output,
    parse_registered_ont_inventory,
)
from app.services.network.parsers.loader import parse_ont_info_detail

CORPUS = Path(__file__).parent / "transcripts" / "huawei"


def _classify(raw: str) -> dict[str, Any]:
    response = classify_huawei_cli_response(raw)
    return {
        "error_code": response.error_code.name,
        "has_error_marker": response.has_error_marker,
        "resource_absent": is_huawei_resource_absent(raw, HuaweiCliResource.ONT),
    }


def _port_inventory(raw: str) -> list[dict[str, Any]]:
    return [asdict(e) for e in parse_registered_ont_inventory(raw, "0/2/1")]


def _by_sn(raw: str) -> dict[str, Any]:
    detail = parse_ont_info_detail(normalize_inventory_output(raw))
    return {
        "classification": _classify(raw),
        "detail": asdict(detail) if detail else None,
    }


# command file stem -> parser. Paging-off rejections have no parser of their
# own; the CLI-response classifier must flag them as errors.
PARSERS: dict[str, Callable[[str], Any]] = {
    "display_ont_info_port_all": _port_inventory,
    "display_ont_info_by_sn": _by_sn,
    "screen_length_0_temporary_rejected": _classify,
}

TRANSCRIPTS = sorted(CORPUS.glob("*/*.txt"))


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


def test_corpus_is_not_empty() -> None:
    assert len(TRANSCRIPTS) >= 4


@pytest.mark.parametrize("path", TRANSCRIPTS, ids=_id)
def test_transcript_matches_golden(path: Path) -> None:
    assert path.stem in PARSERS, f"register a parser for {_id(path)}"
    actual = PARSERS[path.stem](path.read_text(encoding="utf-8"))
    golden = path.with_suffix(".expected.json")
    if os.environ.get("UPDATE_TRANSCRIPT_GOLDENS") == "1":
        golden.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    assert golden.exists(), f"missing golden {golden.name}"
    assert actual == json.loads(golden.read_text())


@pytest.mark.parametrize(
    "path", [p for p in TRANSCRIPTS if p.stem.endswith("_rejected")], ids=_id
)
def test_paging_rejections_are_errors(path: Path) -> None:
    assert _classify(path.read_text())["has_error_marker"] is True
