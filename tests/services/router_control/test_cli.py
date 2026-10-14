"""The read-only CLI adapter (database is the sqlite test session)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.router_management import (
    Router,
    RouterConfigSnapshot,
    RouterSnapshotSource,
)
from scripts.network import router_control_report as cli
from tests.services.router_control.support import SERVICES_OK, export

SCRIPT = Path(cli.__file__)


@pytest.fixture
def run_cli(db_session, monkeypatch, capsys):
    @contextmanager
    def session():
        yield db_session

    monkeypatch.setattr(cli, "read_only_snapshot_session", session)

    def run(*argv: str):
        code = cli.main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def make_router(db, name: str, text: str | None, *, active: bool = True) -> Router:
    router = Router(
        name=name,
        hostname=name,
        management_ip="192.0.2.20",
        rest_api_username="u",
        rest_api_password="p",
        is_active=active,
    )
    db.add(router)
    db.flush()
    if text is not None:
        db.add(
            RouterConfigSnapshot(
                router_id=router.id,
                config_export=text,
                config_hash="0" * 64,
                source=RouterSnapshotSource.scheduled,
                created_at=datetime.now(UTC),
            )
        )
        db.flush()
    return router


def write(tmp_path: Path, name: str, data: object) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def test_default_assignment_uses_the_health_role_and_sites_file(
    db_session, run_cli, tmp_path
):
    make_router(db_session, "r-a", export())
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    code, out, _ = run_cli("--sites", sites, "--json")
    data = json.loads(out)
    assert code == 0 and data["exit_code"] == 0
    router = data["routers"][0]
    assert router["profiles"] == ["access"] and router["site"] == "site-a"
    assert router["modules"]["ntp"]["status"] == "conform"


def test_drift_exit_code_and_text_output(db_session, run_cli, tmp_path):
    make_router(db_session, "r-a", export(services=SERVICES_OK.split("/tool")[0]))
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    code, out, _ = run_cli("--sites", sites)
    assert code == 1
    assert "DRIFT drifted /btest/enabled" in out and "exit 1" in out


def test_no_snapshot_exits_3_and_inactive_routers_are_ignored(
    db_session, run_cli, tmp_path
):
    make_router(db_session, "r-a", None)
    make_router(db_session, "r-off", export(), active=False)
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    code, out, _ = run_cli("--sites", sites, "--json")
    assert code == 3
    assert [r["router"] for r in json.loads(out)["routers"]] == ["r-a"]


def test_unknown_router_and_bad_inputs_are_refused(db_session, run_cli, tmp_path):
    make_router(db_session, "r-a", export())
    code, _, err = run_cli("--router", "nope")
    assert code == 2 and "unknown router: nope" in err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code, _, err = run_cli("--layers", str(bad))
    assert code == 2 and "refused" in err
    code, _, err = run_cli("--today", "yesterday")
    assert code == 2


def test_private_layers_assignments_and_exceptions(db_session, run_cli, tmp_path):
    router = make_router(
        db_session, "r-a", export(services=SERVICES_OK.split("/tool")[0])
    )
    layers = write(
        tmp_path,
        "layers.json",
        {
            "layers": [
                {
                    "id": "g1",
                    "tier": "global",
                    "name": "private-baseline",
                    "weight": 0,
                    "document": {
                        "schema": 1,
                        "modules": {
                            "ntp": {
                                "module_version": 1,
                                "mode": "enforce",
                                "params": {
                                    "client_enabled": True,
                                    "servers": ["192.0.2.92"],
                                },
                            },
                            "services-hardening": {
                                "module_version": 1,
                                "mode": "enforce",
                                "params": {
                                    "disabled_services": ["telnet"],
                                    "btest": {"enabled": False, "authenticate": True},
                                },
                            },
                        },
                    },
                }
            ]
        },
    )
    assignments = write(
        tmp_path,
        "assign.json",
        {"routers": {"r-a": {"site": "site-a", "profiles": ["access"]}}},
    )
    exceptions = write(
        tmp_path,
        "exc.json",
        {
            "exceptions": [
                {
                    "id": "X-1",
                    "module": "services-hardening",
                    "kind": "override",
                    "router": "r-a",
                    "reason": "bandwidth tests from management ranges",
                    "created_on": "2026-10-14",
                    "until": "2026-11-13",
                    "path": "/btest",
                    "patch": {"enabled": True, "authenticate": True},
                    "widening": True,
                    "approved": True,
                }
            ]
        },
    )
    code, out, _ = run_cli(
        "--layers",
        layers,
        "--assignments",
        assignments,
        "--exceptions",
        exceptions,
        "--today",
        "2026-10-20",
        "--json",
    )
    data = json.loads(out)
    assert code == 0, data
    assert data["seed"].startswith("private layers")
    assert set(data["routers"][0]["modules"]) == {"ntp", "services-hardening"}
    code, out, _ = run_cli(
        "--layers",
        layers,
        "--assignments",
        assignments,
        "--exceptions",
        exceptions,
        "--today",
        "2026-11-15",
        "--json",
    )
    assert code == 1  # the exception lapsed, so the drift is visible again
    assert json.loads(out)["routers"][0]["lapsed"][0]["finding"] == "exception_lapsed"
    assert router.name == "r-a"


def test_the_script_is_structurally_read_only():
    source = SCRIPT.read_text()
    assert "read_only_snapshot_session()" in source
    for forbidden in (
        ".commit(",
        ".add(",
        ".delete(",
        "execute(",
        "paramiko",
        "librouteros",
        "requests",
        "httpx",
    ):
        assert forbidden not in source, forbidden


def test_cli_does_not_write_to_the_database(db_session, run_cli, tmp_path):
    make_router(db_session, "r-a", export())
    db_session.commit()
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    run_cli("--sites", sites)
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_today_defaults_to_the_application_timezone_not_utc():
    from datetime import date

    # 23:30 UTC on 14 Oct is already 00:30 on 15 Oct in Africa/Lagos (UTC+1)
    late = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)
    assert cli.today_in_app_timezone(late) == date(2026, 10, 15)
    assert late.date() == date(2026, 10, 14)  # what the old code used


def test_main_uses_the_app_timezone_date(db_session, run_cli, monkeypatch, tmp_path):
    from datetime import date

    make_router(db_session, "r-a", export())
    monkeypatch.setattr(cli, "today_in_app_timezone", lambda: date(2026, 10, 15))
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    code, out, _ = run_cli("--sites", sites, "--json")
    assert json.loads(out)["today"] == "2026-10-15" and code == 0


def test_an_exception_lapses_on_the_lagos_date_not_the_utc_date(
    db_session, run_cli, monkeypatch, tmp_path
):
    """At 23:30 UTC on `until` it is already the day after in Lagos: lapsed."""
    from datetime import date

    router = make_router(
        db_session, "r-a", export(services=SERVICES_OK.split("/tool")[0])
    )
    exceptions = write(
        tmp_path,
        "exc.json",
        {
            "exceptions": [
                {
                    "id": "X-1",
                    "module": "services-hardening",
                    "kind": "override",
                    "router": "r-a",
                    "reason": "bandwidth tests from management ranges",
                    "created_on": "2026-10-14",
                    "until": "2026-10-14",
                    "path": "/btest",
                    "patch": {"enabled": True, "authenticate": True},
                    "approved": True,
                }
            ]
        },
    )
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    late = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)
    monkeypatch.setattr(cli, "today_in_app_timezone", lambda: cli_today_for(late))
    code, out, _ = run_cli("--sites", sites, "--exceptions", exceptions, "--json")
    data = json.loads(out)
    assert data["today"] == "2026-10-15"
    assert [x["id"] for x in data["routers"][0]["lapsed"]] == ["X-1"]
    assert code == 1 and router.name == "r-a"
    assert date.fromisoformat(data["today"]) > date(2026, 10, 14)


def cli_today_for(moment):
    from app.services.router_health.policy import today_in_app_timezone

    return today_in_app_timezone(moment)


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"routers": ["not", "an", "object"]}),
        json.dumps({"routers": {"r-a": "not an object"}}),
        json.dumps({"routers": {"r-a": {"profiles": "access"}}}),
        json.dumps([]),
    ],
)
def test_malformed_assignments_file_exits_2_never_1(
    db_session, run_cli, tmp_path, content
):
    make_router(db_session, "r-a", export(services=SERVICES_OK.split("/tool")[0]))
    path = tmp_path / "assign.json"
    path.write_text(content)
    code, out, err = run_cli("--assignments", str(path))
    assert code == 2, (out, err)
    assert "refused" in err and out == ""


def test_unhandled_errors_exit_2_without_leaking_the_message(
    db_session, run_cli, monkeypatch
):
    make_router(db_session, "r-a", export())

    def boom(*args, **kwargs):
        raise RuntimeError("password=hunter2 leaked from a driver")

    monkeypatch.setattr(cli.report, "build_fleet_report", boom)
    code, out, err = run_cli()
    assert code == 2 and out == ""
    assert "RuntimeError" in err and "hunter2" not in err


def test_database_errors_exit_2(monkeypatch):
    @contextmanager
    def broken():
        raise ConnectionError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "read_only_snapshot_session", broken)
    code, _, err = cli_run_main(monkeypatch)
    assert code == 2 and "ConnectionError" in err


def cli_run_main(monkeypatch):
    import io
    import sys

    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    code = cli.main([])
    return code, "", err.getvalue()


def test_default_assignment_says_the_self_reference_check_cannot_run(
    db_session, run_cli, tmp_path
):
    make_router(db_session, "r-a", export())
    sites = write(tmp_path, "sites.json", {"r-a": "site-a"})
    code, out, err = run_cli("--sites", sites)
    assert "self-reference check cannot run" in err
    assert "NOT CHECKED" in out
    # a declared loopback removes both statements
    assignments = write(
        tmp_path,
        "assign.json",
        {
            "routers": {
                "r-a": {
                    "site": "site-a",
                    "profiles": ["access"],
                    "loopback_v4": "198.51.100.97",
                }
            }
        },
    )
    code, out, err = run_cli("--assignments", assignments)
    assert "cannot run" not in err and "NOT CHECKED" not in out
