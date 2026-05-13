from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.network.profile_apply_workflow import (
    AppliedCommand,
    ProfileApplyError,
    ProfileCommandGroup,
    apply_profile_bundle,
    build_profile_apply_plan,
)


def _plan():
    return build_profile_apply_plan(
        "gold-50m",
        [
            ProfileCommandGroup(
                step="Create DBA",
                commands=("dba-profile add profile-id 100 profile-name DBA_50M",),
            ),
            ProfileCommandGroup(
                step="Create traffic table",
                commands=(
                    "traffic table ip index 100 name TT_50M cir 50000 pir 50000",
                ),
            ),
        ],
    )


def test_build_profile_apply_plan_accepts_command_set_like_objects() -> None:
    command_set = SimpleNamespace(
        step="Create service profile",
        commands=["ont-srvprofile gpon profile-id 100 profile-name HG8546M", "commit"],
        requires_config_mode=True,
    )

    plan = build_profile_apply_plan("bundle", [command_set])

    assert plan.name == "bundle"
    assert plan.commands == (
        "ont-srvprofile gpon profile-id 100 profile-name HG8546M",
        "commit",
    )


def test_build_profile_apply_plan_rejects_empty_plan() -> None:
    with pytest.raises(ProfileApplyError, match="no command groups"):
        build_profile_apply_plan("empty", [])


def test_build_profile_apply_plan_rejects_multiline_command() -> None:
    with pytest.raises(ProfileApplyError, match="multi-line"):
        build_profile_apply_plan(
            "bad",
            [ProfileCommandGroup(step="Bad", commands=("display version\nsave",))],
        )


def test_apply_profile_bundle_dry_run_does_not_backup_or_execute() -> None:
    calls: list[str] = []

    result = apply_profile_bundle(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        olt=SimpleNamespace(id=uuid4(), name="Garki"),  # type: ignore[arg-type]
        plan=_plan(),
        actor_is_admin=True,
        dry_run=True,
        backup_runner=lambda *_args: calls.append("backup"),  # type: ignore[arg-type,return-value]
        command_executor=lambda *_args: calls.append("execute"),  # type: ignore[arg-type,return-value]
    )

    assert result.success is True
    assert result.dry_run is True
    assert result.commands == _plan().commands
    assert calls == []


def test_apply_profile_bundle_requires_admin_by_default() -> None:
    result = apply_profile_bundle(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        olt=SimpleNamespace(id=uuid4(), name="Garki"),  # type: ignore[arg-type]
        plan=_plan(),
        actor_is_admin=False,
        dry_run=False,
    )

    assert result.success is False
    assert result.errors == ("admin_required",)


def test_apply_profile_bundle_runs_backup_before_live_execution() -> None:
    calls: list[str] = []
    backup = SimpleNamespace(id=uuid4())

    def backup_runner(_db, _olt_id):
        calls.append("backup")
        return backup, "backup ok"

    def command_executor(_olt, plan):
        calls.append("execute")
        return [
            AppliedCommand(command=command, success=True, message="ok")
            for command in plan.commands
        ]

    result = apply_profile_bundle(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        olt=SimpleNamespace(id=uuid4(), name="Garki"),  # type: ignore[arg-type]
        plan=_plan(),
        actor_is_admin=True,
        dry_run=False,
        backup_runner=backup_runner,  # type: ignore[arg-type]
        command_executor=command_executor,  # type: ignore[arg-type]
    )

    assert result.success is True
    assert result.backup_id == str(backup.id)
    assert calls == ["backup", "execute"]


def test_apply_profile_bundle_stops_when_backup_fails() -> None:
    calls: list[str] = []

    def command_executor(_olt, _plan):
        calls.append("execute")
        return []

    result = apply_profile_bundle(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        olt=SimpleNamespace(id=uuid4(), name="Garki"),  # type: ignore[arg-type]
        plan=_plan(),
        actor_is_admin=True,
        dry_run=False,
        backup_runner=lambda *_args: (None, "no ssh"),  # type: ignore[arg-type]
        command_executor=command_executor,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.errors == ("backup_failed",)
    assert calls == []


def test_apply_profile_bundle_reports_failed_command() -> None:
    def command_executor(_olt, plan):
        first, second = plan.commands
        return [
            AppliedCommand(command=first, success=True, message="ok"),
            AppliedCommand(command=second, success=False, message="parameter error"),
        ]

    result = apply_profile_bundle(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        olt=SimpleNamespace(id=uuid4(), name="Garki"),  # type: ignore[arg-type]
        plan=_plan(),
        actor_is_admin=True,
        dry_run=False,
        require_backup=False,
        command_executor=command_executor,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert "traffic table" in result.message
    assert result.errors == ("parameter error",)


def test_apply_profile_bundle_reports_incomplete_execution() -> None:
    def command_executor(_olt, plan):
        return [AppliedCommand(command=plan.commands[0], success=True, message="ok")]

    result = apply_profile_bundle(
        db=SimpleNamespace(),  # type: ignore[arg-type]
        olt=SimpleNamespace(id=uuid4(), name="Garki"),  # type: ignore[arg-type]
        plan=_plan(),
        actor_is_admin=True,
        dry_run=False,
        require_backup=False,
        command_executor=command_executor,  # type: ignore[arg-type]
    )

    assert result.success is False
    assert result.errors == ("incomplete_execution",)
