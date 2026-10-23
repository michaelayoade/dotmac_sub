"""The restore drill asserts the migrator/owner edge in the new direction."""

from __future__ import annotations

import subprocess
from typing import Any

from scripts import restore_drill as drill

ROLES = "\n".join(
    f"{name}|true|{str(bypass).lower()}|false"
    for name, bypass in (
        ("dotmac_app", False),
        ("app_admin", True),
        ("app_user", False),
        ("platform_api", False),
        ("outbox_dispatcher", False),
        ("platform_outbox_dispatcher", False),
    )
)


class FakePg:
    """Answers only the isolation-model queries, with a chosen edge."""

    def __init__(self, edge: str) -> None:
        self.edge = edge

    def scalar(self, sql_text: str, *, db: str = "") -> str:
        if "FROM pg_roles" in sql_text:
            return ROLES
        if "relrowsecurity" in sql_text and "count(*) FILTER" in sql_text:
            return "1|0"
        if "pg_policies" in sql_text:
            return "0"
        if "to_regclass" in sql_text:
            return "f"
        if "count(DISTINCT tableowner)" in sql_text:
            return "2|1|3"
        if "pg_has_role('app_admin', 'dotmac_app', 'USAGE')" in sql_text:
            return self.edge
        raise AssertionError(sql_text)

    def psql(self, *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("no probe is expected when the planes are absent")


def _direction_check(edge: str) -> drill.RestoreCheck:
    checks = drill.assert_isolation_model(
        FakePg(edge),  # type: ignore[arg-type]
        globals_roles={"dotmac_app", "app_admin"},
    )
    (check,) = [c for c in checks if c.name == "migrator_owner_direction"]
    return check


def test_new_direction_passes() -> None:
    assert _direction_check("true|true|false|false").passed


def test_old_direction_fails_the_drill() -> None:
    check = _direction_check("true|true|true|false")
    assert not check.passed
    assert "dotmac_app reaches app_admin: True" in check.detail


def test_missing_edge_fails_the_drill() -> None:
    assert not _direction_check("false|false|false|false").passed


def test_admin_option_fails_the_drill() -> None:
    assert not _direction_check("true|true|false|true").passed


def test_fixture_builds_the_new_edge_only() -> None:
    assert "GRANT dotmac_app TO app_admin WITH INHERIT TRUE, SET TRUE;" in (
        drill.FIXTURE_SQL
    )
    assert "GRANT app_admin TO dotmac_app" not in drill.FIXTURE_SQL
