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
