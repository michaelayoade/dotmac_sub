"""The operator planner accepts explicit, provenance-bearing funding facts."""

from __future__ import annotations

import json

import pytest

from scripts.one_off.plan_prepaid_balance_sweep import _load_funding_snapshot


def test_load_funding_snapshot_preserves_provenance_and_decimal_money(tmp_path):
    path = tmp_path / "funding.json"
    path.write_text(
        json.dumps(
            {
                "source": "splynx-cutover-plus-native-events:prod-2026-07-14",
                "captured_at": "2026-07-14T12:08:25Z",
                "accounts": [
                    {
                        "account_id": "f7a996e4-8a25-4c33-9d73-e69da71cf406",
                        "available_balance": "123.45",
                        "required_balance": "5000.00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    snapshot = _load_funding_snapshot(str(path))

    assert snapshot.source == "splynx-cutover-plus-native-events:prod-2026-07-14"
    assert snapshot.captured_at.isoformat() == "2026-07-14T12:08:25+00:00"
    assert str(snapshot.decisions[0].available_balance) == "123.45"
    assert str(snapshot.decisions[0].required_balance) == "5000.00"


@pytest.mark.parametrize("amount", [None, "NaN", "Infinity", "not-money"])
def test_load_funding_snapshot_rejects_non_finite_or_missing_money(tmp_path, amount):
    path = tmp_path / "funding.json"
    path.write_text(
        json.dumps(
            {
                "source": "cutover-reconstruction",
                "captured_at": "2026-07-14T12:08:25+00:00",
                "accounts": [
                    {
                        "account_id": "f7a996e4-8a25-4c33-9d73-e69da71cf406",
                        "available_balance": amount,
                        "required_balance": "5000.00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="available_balance"):
        _load_funding_snapshot(str(path))


def test_load_funding_snapshot_requires_timezone(tmp_path):
    path = tmp_path / "funding.json"
    path.write_text(
        json.dumps(
            {
                "source": "cutover-reconstruction",
                "captured_at": "2026-07-14T12:08:25",
                "accounts": [
                    {
                        "account_id": "f7a996e4-8a25-4c33-9d73-e69da71cf406",
                        "available_balance": "0.00",
                        "required_balance": "5000.00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="timezone"):
        _load_funding_snapshot(str(path))
