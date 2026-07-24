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


def test_save_secret_can_add_indexed_field_without_exposing_existing_values(
    monkeypatch,
):
    written = {}
    monkeypatch.setattr(
        "app.services.secrets.read_secret_fields",
        lambda _path: {"secret_key": "existing-value"},
    )
    monkeypatch.setattr(
        "app.services.secrets.list_secret_field_names",
        lambda _path: ["secret_key"],
    )
    monkeypatch.setattr(
        "app.services.secrets.write_secret",
        lambda path, fields: written.update(path=path, fields=fields) or True,
    )

    result = web_system_secrets.save_secret(
        "integrations/paystack",
        {
            "field_secret_key": "",
            "new_key_0": "public_key",
            "new_value_0": "new-public-value",
        },
    )

    assert result["ok"] is True
    assert written == {
        "path": "integrations/paystack",
        "fields": {
            "secret_key": "existing-value",
            "public_key": "new-public-value",
        },
    }
