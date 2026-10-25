"""Run every captured Huawei CLI transcript through its parser vs a golden.

Layout: tests/transcripts/huawei/<model>/<command>.txt with a sibling
<command>.expected.json. See tests/transcripts/README.md. A transcript with no
registered parser, or no golden, fails the test so captures cannot be added
silently. Goldens are hand-verified and are never regenerated from the parser.
"""

from __future__ import annotations

import json
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
    assert golden.exists(), f"missing golden {golden.name}"
    assert actual == json.loads(golden.read_text())


@pytest.mark.parametrize(
    "path", [p for p in TRANSCRIPTS if p.stem.endswith("_rejected")], ids=_id
)
def test_paging_rejections_are_errors(path: Path) -> None:
    assert _classify(path.read_text())["has_error_marker"] is True


# --- parse_ont_info fail-closed rule (rows present but none parse) -----------

GARKI = CORPUS / "MA5800-X2" / "display_ont_info_port_all.txt"


def test_parse_ont_info_reads_paged_garki_transcript() -> None:
    from app.services.network.parsers.loader import parse_ont_info

    result = parse_ont_info(GARKI.read_text())
    assert result.success is True
    assert result.row_count == 16
    assert {e.fsp for e in result.data} == {"0/2/1"}
    assert result.data[0].serial_number == "48575443FAA65084"


def test_parse_ont_info_regex_fallback_when_template_misses(monkeypatch) -> None:
    from app.services.network.parsers import loader

    monkeypatch.setattr(loader, "_parse_with_template", lambda *_a, **_k: ([], []))
    result = loader.parse_ont_info(GARKI.read_text())
    assert result.success is True
    assert result.row_count == 16
    assert "regex fallback" in " ".join(result.warnings)


def test_parse_ont_info_unreadable_rows_are_failure_not_empty(monkeypatch) -> None:
    import re

    from app.services.network.olt_ssh_ont import status
    from app.services.network.parsers import loader

    # Rows are present (hint matches) but neither the template nor the row
    # fallback can read them: must be a failure, never an empty success.
    monkeypatch.setattr(loader, "_parse_with_template", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(status, "_ONT_ROW_FALLBACK_RE", re.compile(r"(?!x)x"))
    assert status.inventory_has_data_rows(GARKI.read_text())
    result = loader.parse_ont_info(GARKI.read_text())
    assert result.data == []
    assert result.success is False


def test_parse_ont_info_explicit_empty_is_still_success() -> None:
    from app.services.network.parsers.loader import parse_ont_info

    result = parse_ont_info("  Failure: No ONT is configured\nboi-olt#\n")
    assert result.success is True and result.data == []
