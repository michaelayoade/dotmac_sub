from app.services import web_system_secrets


def test_secret_edit_context_never_contains_existing_values(monkeypatch):
    monkeypatch.setattr("app.services.secrets.is_openbao_available", lambda: True)
    monkeypatch.setattr(
        "app.services.secrets.list_secret_field_names",
        lambda _path: ["api_key", "password"],
    )
    monkeypatch.setattr(
        "app.services.secrets.read_secret_metadata",
        lambda _path: {"current_version": 2},
    )

    context = web_system_secrets.build_secret_edit_context("provider")

    assert context["fields"] == {
        "api_key": web_system_secrets.MASKED_SECRET_VALUE,
        "password": web_system_secrets.MASKED_SECRET_VALUE,
    }
