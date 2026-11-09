"""Explicit synthetic command context for settings-owner unit tests."""

from app.services.domain_settings import ADMIN_SETTINGS_FORM_WRITE_SCOPE
from app.services.owner_commands import CommandContext


def settings_context() -> CommandContext:
    return CommandContext.system(
        actor="user:pytest-settings-admin",
        scope=ADMIN_SETTINGS_FORM_WRITE_SCOPE,
        reason="test registered settings editor",
    )
