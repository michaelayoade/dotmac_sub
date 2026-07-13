from __future__ import annotations

import pytest

from scripts.backup.pgbackrest_info import BackupHealthError, evaluate_info


def _payload(*, stop: int = 1_000, status_code: int = 0, backups=None):
    return [
        {
            "name": "dotmac-sub",
            "status": {
                "code": status_code,
                "message": "ok" if status_code == 0 else "error",
            },
            "repo": [{"key": 1, "status": {"code": status_code, "message": "ok"}}],
            "backup": backups
            if backups is not None
            else [
                {
                    "label": "20260713-010000F_20260713-060000I",
                    "type": "incr",
                    "error": False,
                    "timestamp": {"start": stop - 20, "stop": stop},
                }
            ],
        }
    ]


def test_evaluate_info_returns_newest_completed_backup():
    payload = _payload(
        backups=[
            {"label": "oldF", "type": "full", "timestamp": {"stop": 900}},
            {"label": "newI", "type": "incr", "timestamp": {"stop": 1_000}},
            {"label": "runningI", "type": "incr", "timestamp": {"start": 1_100}},
        ]
    )

    health = evaluate_info(payload, stanza="dotmac-sub", max_age_seconds=200, now=1_100)

    assert health.label == "newI"
    assert health.backup_type == "incr"
    assert health.completed_at == 1_000
    assert health.age_seconds == 100


def test_evaluate_info_rejects_stale_backup():
    with pytest.raises(BackupHealthError, match="backup is stale"):
        evaluate_info(_payload(), stanza="dotmac-sub", max_age_seconds=99, now=1_100)


def test_evaluate_info_rejects_unhealthy_repository():
    payload = _payload()
    payload[0]["repo"][0]["status"] = {"code": 1, "message": "repo unavailable"}
    with pytest.raises(BackupHealthError, match="repository is unhealthy"):
        evaluate_info(payload, stanza="dotmac-sub", max_age_seconds=200, now=1_100)


def test_evaluate_info_rejects_missing_completed_backup():
    with pytest.raises(BackupHealthError, match="no completed backup"):
        evaluate_info(
            _payload(
                backups=[
                    {"label": "running", "type": "full", "timestamp": {"start": 1_000}}
                ]
            ),
            stanza="dotmac-sub",
            max_age_seconds=200,
            now=1_100,
        )
