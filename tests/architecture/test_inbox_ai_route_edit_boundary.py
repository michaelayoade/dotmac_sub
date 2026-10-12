"""Existing AI route editing stays within the typed canonical writer."""

from pathlib import Path


def test_ai_route_update_has_one_owner_executor_and_no_legacy_patch_bag():
    source = Path("app/services/team_inbox_commands.py").read_text(encoding="utf-8")
    owner = source.split("def update_ai_route(", 1)[1].split("def delete_ai_route", 1)[
        0
    ]
    assert "command: UpdateAiRouteCommand" in owner
    assert owner.count("execute_owner_command(") == 1
    assert "patch=command.patch" in owner
    assert "_commit(" not in owner
    participant = (
        Path("app/services/team_inbox_routing.py")
        .read_text(encoding="utf-8")
        .split("def update_ai_route(", 1)[1]
        .split("def delete_ai_route", 1)[0]
    )
    assert "patch: AiRoutePatch" in participant
    assert ".with_for_update()" in participant
    assert "route_changed" in participant
    assert ".commit(" not in participant
    assert "TeamInboxAiRoute(" not in participant
