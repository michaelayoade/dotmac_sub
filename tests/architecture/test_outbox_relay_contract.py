"""Static guards for the outbox relay (SUB-2): isolation, plane and deploy wiring."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "app" / "outbox_relay"


def _compose_service() -> dict[str, object]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["outbox-relay"]
    assert isinstance(service, dict)
    return service


def test_relay_never_imports_application_config_or_database_owners() -> None:
    banned = ("app.config", "app.db", "app.services", "app.models")
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert not name.startswith(banned), f"{path.name} imports {name}"


def test_relay_is_tenant_plane_only() -> None:
    for path in PACKAGE.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name in {"preflight.py", "__init__.py"}:
            continue  # preflight names them only to prove they are denied
        assert "platform_outbox" not in source, path.name
        assert "claim_platform" not in source, path.name
        assert "enqueue" not in source, path.name


def test_relay_makes_no_direct_table_access() -> None:
    for path in PACKAGE.glob("*.py"):
        if path.name == "preflight.py":
            continue  # reads pg_class/privilege catalogs only
        source = path.read_text(encoding="utf-8").lower()
        for verb in ("from public.", "from outbox_events", "update public."):
            assert verb not in source, (path.name, verb)
        assert "insert into" not in source and "delete from" not in source


def test_compose_service_has_no_app_credentials_and_is_profile_gated() -> None:
    service = _compose_service()
    assert "env_file" not in service
    assert service["profiles"] == ["outbox-relay"]
    environment = service["environment"]
    assert isinstance(environment, dict)
    assert "DATABASE_URL" not in environment
    assert "OPENBAO_TOKEN" not in environment
    assert "OUTBOX_RELAY_DATABASE_URL" in environment
    assert "ports" not in service
    assert service["command"] == ["python", "-m", "app.outbox_relay"]


def test_descriptor_role_declares_only_the_relay_materials() -> None:
    descriptor = tomllib.loads((ROOT / "deploy/product.toml").read_text())
    (role,) = [r for r in descriptor["roles"] if r["code"] == "outbox-relay"]
    assert role["materials"] == [
        "OUTBOX_RELAY_DATABASE_URL",
        "OUTBOX_RELAY_TENANT_DATABASE_URL",
    ]
    assert role["command"] == ["python", "-m", "app.outbox_relay"]


def test_deploy_never_creates_the_relay_on_a_host_that_has_not_run_it() -> None:
    from scripts.deploy_app_image_services import REQUIRED_APP_SERVICES

    assert "outbox-relay" not in REQUIRED_APP_SERVICES


def test_dead_letter_alert_rule_exists() -> None:
    rules = yaml.safe_load(
        (ROOT / "deploy/observability/outbox_relay.rules.yml").read_text()
    )
    alerts = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    assert (
        "outbox_relay_dead_lettered_total" in alerts["OutboxRelayDeadLettered"]["expr"]
    )
    assert alerts["OutboxRelayDeadLettered"]["labels"]["severity"] == "critical"


def test_backlog_alerts_use_the_database_side_snapshot_not_a_bare_absent() -> None:
    rules = yaml.safe_load(
        (ROOT / "deploy/observability/outbox_relay.rules.yml").read_text()
    )
    alerts = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    queued = alerts["OutboxEventsQueuedRelayNotRunning"]["expr"]
    assert 'domain="outbox_backlog"' in queued
    assert "oldest_pending_age_seconds" in queued
    assert "absent(outbox_relay_last_poll_timestamp_seconds)" in queued
    for name in ("OutboxEventTypeParked", "OutboxEventTypeParkedLong"):
        assert "by (event_type)" in alerts[name]["expr"]
    assert alerts["OutboxEventTypeParkedLong"]["labels"]["severity"] == "critical"


def test_backlog_task_is_scheduled_and_has_a_reliability_contract() -> None:
    task = "app.tasks.outbox_backlog.publish_outbox_backlog"
    assert task in (ROOT / "app/services/scheduler_config.py").read_text()
    assert task in (ROOT / "app/services/task_reliability.py").read_text()
    assert '"outbox_backlog"' in (ROOT / "app/services/observability.py").read_text()


def test_the_sub_local_loop_is_a_named_deletion_registered_deviation() -> None:
    assert "NAMED DEVIATION D6" in (PACKAGE / "dispatcher.py").read_text()
    assert "Deviation D6" in (ROOT / "docs/runbooks/OUTBOX_RELAY.md").read_text()
