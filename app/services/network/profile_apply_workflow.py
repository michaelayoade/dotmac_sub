"""Admin-gated workflow for applying generated OLT profile commands."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models.network import OltConfigBackup, OLTDevice
from app.services.network import olt_operations
from app.services.network.olt_ssh_session import CliMode, CommandResult, olt_session


class ProfileApplyError(ValueError):
    """Raised when a profile apply plan is invalid."""


@dataclass(frozen=True)
class ProfileCommandGroup:
    """One ordered group of OLT commands for a profile bundle."""

    step: str
    commands: tuple[str, ...]
    requires_config_mode: bool = True


@dataclass(frozen=True)
class ProfileApplyPlan:
    """A complete profile apply plan, normally generated from a bundle."""

    name: str
    groups: tuple[ProfileCommandGroup, ...]

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(command for group in self.groups for command in group.commands)


@dataclass(frozen=True)
class AppliedCommand:
    """Result for a single executed command."""

    command: str
    success: bool
    message: str = ""
    output: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class ProfileApplyResult:
    """Outcome of dry-run or live profile apply."""

    success: bool
    message: str
    dry_run: bool
    commands: tuple[str, ...] = ()
    backup_id: str | None = None
    applied_commands: tuple[AppliedCommand, ...] = ()
    errors: tuple[str, ...] = ()


BackupRunner = Callable[
    [Session, str],
    tuple[OltConfigBackup | None, str],
]
CommandExecutor = Callable[[OLTDevice, ProfileApplyPlan], Iterable[AppliedCommand]]


def build_profile_apply_plan(
    name: str,
    command_groups: Iterable[Any],
) -> ProfileApplyPlan:
    """Build a validated apply plan from command groups or OltCommandSet-like objects."""
    groups: list[ProfileCommandGroup] = []
    for raw_group in command_groups:
        step = str(getattr(raw_group, "step", "") or "").strip()
        commands = tuple(str(command).strip() for command in getattr(raw_group, "commands", ()))
        requires_config_mode = bool(getattr(raw_group, "requires_config_mode", True))
        groups.append(
            ProfileCommandGroup(
                step=step or "Apply profile commands",
                commands=commands,
                requires_config_mode=requires_config_mode,
            )
        )
    plan = ProfileApplyPlan(name=str(name or "").strip() or "OLT profile bundle", groups=tuple(groups))
    validate_profile_apply_plan(plan)
    return plan


def validate_profile_apply_plan(plan: ProfileApplyPlan) -> None:
    """Fail fast on empty, malformed, or multi-line command plans."""
    if not plan.groups:
        raise ProfileApplyError("Profile apply plan has no command groups")
    if not plan.commands:
        raise ProfileApplyError("Profile apply plan has no commands")
    for group in plan.groups:
        if not group.commands:
            raise ProfileApplyError(f"Command group '{group.step}' has no commands")
        for command in group.commands:
            if not command:
                raise ProfileApplyError(f"Command group '{group.step}' contains an empty command")
            if any(char in command for char in ("\r", "\n", "\x00")):
                raise ProfileApplyError(
                    f"Command group '{group.step}' contains a multi-line command"
                )


def _default_command_executor(
    olt: OLTDevice,
    plan: ProfileApplyPlan,
) -> Iterable[AppliedCommand]:
    with olt_session(olt) as session:
        for group in plan.groups:
            require_mode = CliMode.CONFIG if group.requires_config_mode else None
            results = session.run_commands(
                list(group.commands),
                require_mode=require_mode,
                stop_on_error=True,
            )
            for command, result in zip(group.commands, results, strict=False):
                yield _applied_command_from_result(command, result)
            if any(not result.success for result in results):
                return


def _applied_command_from_result(command: str, result: CommandResult) -> AppliedCommand:
    return AppliedCommand(
        command=command,
        success=result.success,
        message=result.message,
        output=result.output,
        error_code=result.error_code.value,
    )


def apply_profile_bundle(
    db: Session,
    olt: OLTDevice,
    plan: ProfileApplyPlan,
    *,
    actor_is_admin: bool,
    dry_run: bool = True,
    require_admin: bool = True,
    require_backup: bool = True,
    backup_runner: BackupRunner = olt_operations.backup_running_config_ssh,
    command_executor: CommandExecutor = _default_command_executor,
) -> ProfileApplyResult:
    """Apply generated profile commands with admin and backup guardrails.

    The function defaults to dry-run. Live execution requires an admin actor when
    ``require_admin`` is true, and a successful running-config backup when
    ``require_backup`` is true.
    """
    validate_profile_apply_plan(plan)

    if require_admin and not actor_is_admin:
        return ProfileApplyResult(
            success=False,
            message="Only admin users can apply OLT profile bundles",
            dry_run=dry_run,
            commands=plan.commands,
            errors=("admin_required",),
        )

    if dry_run:
        return ProfileApplyResult(
            success=True,
            message=f"Dry-run profile apply plan for {plan.name}",
            dry_run=True,
            commands=plan.commands,
        )

    backup_id: str | None = None
    if require_backup:
        backup, backup_message = backup_runner(db, str(olt.id))
        if backup is None:
            return ProfileApplyResult(
                success=False,
                message=f"Backup failed before profile apply: {backup_message}",
                dry_run=False,
                commands=plan.commands,
                errors=("backup_failed",),
            )
        backup_id = str(backup.id)

    applied = tuple(command_executor(olt, plan))
    failed = tuple(command for command in applied if not command.success)
    if failed:
        return ProfileApplyResult(
            success=False,
            message=f"Profile apply failed at command: {failed[0].command}",
            dry_run=False,
            commands=plan.commands,
            backup_id=backup_id,
            applied_commands=applied,
            errors=tuple(command.message or command.error_code for command in failed),
        )

    if len(applied) != len(plan.commands):
        return ProfileApplyResult(
            success=False,
            message="Profile apply stopped before all commands completed",
            dry_run=False,
            commands=plan.commands,
            backup_id=backup_id,
            applied_commands=applied,
            errors=("incomplete_execution",),
        )

    return ProfileApplyResult(
        success=True,
        message=f"Applied profile bundle {plan.name}",
        dry_run=False,
        commands=plan.commands,
        backup_id=backup_id,
        applied_commands=applied,
    )
