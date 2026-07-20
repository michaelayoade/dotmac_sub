from __future__ import annotations

from app.services.network.serial_utils import (
    build_huawei_external_id,
    canonical,
    normalize,
    parse_ont_id_on_olt,
    search_candidates,
)


def test_canonical_matches_ascii_and_hex_huawei_serials() -> None:
    assert canonical("HWTC600AC29C") == "HWTC600AC29C"
    assert canonical("48575443600AC29C") == "HWTC600AC29C"
    assert canonical("48:57:54:43:60:0A:C2:9C") == "HWTC600AC29C"
    assert canonical("HWTC-600A-C29C") == "HWTC600AC29C"


def test_canonical_preserves_non_ascii_hex_prefixes() -> None:
    assert canonical("0011223344556677") == "0011223344556677"
    assert canonical("FFFFFFFF44556677") == "FFFFFFFF44556677"


def test_canonical_preserves_malformed_or_non_hex_values() -> None:
    assert canonical("48575443GGGG1111") == "48575443GGGG1111"
    assert canonical("not-a-real-serial") == "NOTAREALSERIAL"
    assert canonical("") == ""
    assert canonical(None) == ""


def test_search_candidates_include_canonical_counterparts() -> None:
    ascii_candidates = {canonical(value) for value in search_candidates("HWTC600AC29C")}
    hex_candidates = {
        canonical(value) for value in search_candidates("48575443600AC29C")
    }

    assert "HWTC600AC29C" in ascii_candidates
    assert "HWTC600AC29C" in hex_candidates


def test_normalize_still_only_strips_formatting() -> None:
    assert normalize("48575443600AC29C") == "48575443600AC29C"
    assert normalize("HWTC-600A-C29C") == "HWTC600AC29C"


def test_build_huawei_external_id_scopes_ont_id_to_fsp() -> None:
    assert build_huawei_external_id("0/1/5", 0) == "huawei:0.1.5.0"
    assert build_huawei_external_id("0/1/5", "11") == "huawei:0.1.5.11"


def test_parse_ont_id_on_olt_accepts_scoped_huawei_fsp_format() -> None:
    assert parse_ont_id_on_olt("huawei:0.1.5.0") == 0
    assert parse_ont_id_on_olt("huawei:0.1.5.11") == 11
