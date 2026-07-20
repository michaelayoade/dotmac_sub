from app.models.audit import AuditEvent
from app.services import module_manager, web_control_plane


def test_control_plane_covers_all_domains_and_explains_each_row(
    db_session, monkeypatch
):
    db_session.add(
        AuditEvent(
            action="settings.update",
            entity_type="domain_setting",
            entity_id="billing.currency",
            status_code=200,
            is_success=True,
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        web_control_plane,
        "redis_health_check",
        lambda: {"available": True, "checked_at": "2026-07-14T12:00:00Z"},
    )
    monkeypatch.setattr(
        web_control_plane,
        "build_secrets_index_context",
        lambda **_kwargs: {"openbao_available": True, "secrets_list": []},
    )
    monkeypatch.setattr(
        web_control_plane,
        "build_installed_integrations_data",
        lambda _db: {"integrations": []},
    )

    context = web_control_plane.build_control_plane_context(db_session)

    assert [section["id"] for section in context["sections"]] == [
        "settings",
        "rbac",
        "sessions",
        "scheduler",
        "secrets",
        "integrations",
        "webhooks",
    ]
    entries = [entry for section in context["sections"] for entry in section["entries"]]
    assert entries
    required_fields = {
        "effective_value",
        "source",
        "precedence",
        "scope",
        "health",
        "last_change",
    }
    assert all(required_fields <= entry.keys() for entry in entries)
    assert all("history" in section for section in context["sections"])
    settings_section = context["sections"][0]
    assert settings_section["history"][0]["action"] == "settings.update"


def test_secret_display_never_returns_the_secret_value():
    assert web_control_plane._display_value("test-only-sentinel", secret=True) == (
        "Configured"
    )


def test_inert_module_switches_are_not_registered():
    inert = {"inventory", "helpdesk", "scheduling", "voice"}

    assert inert.isdisjoint(module_manager.MODULE_KEY_MAP)
