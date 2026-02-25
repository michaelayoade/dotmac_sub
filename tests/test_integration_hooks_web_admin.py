from app.web.admin import integrations as integrations_web


def test_build_hook_auth_config_bearer_with_extra_json():
    config = integrations_web._build_hook_auth_config(
        auth_type="bearer",
        auth_bearer_token="abc123",
        auth_basic_username=None,
        auth_basic_password=None,
        auth_hmac_secret=None,
        auth_config_json='{"header":"X-Token"}',
    )
    assert config == {"token": "abc123", "header": "X-Token"}


def test_build_hook_auth_config_basic():
    config = integrations_web._build_hook_auth_config(
        auth_type="basic",
        auth_bearer_token=None,
        auth_basic_username="user",
        auth_basic_password="pass",
        auth_hmac_secret=None,
        auth_config_json=None,
    )
    assert config == {"username": "user", "password": "pass"}


def test_hook_form_defaults_applies_template():
    template = {
        "title": "n8n Hook",
        "hook_type": "web",
        "url": "https://n8n.example/hook",
        "http_method": "POST",
        "auth_type": "none",
        "event_filters_csv": "invoice.created",
        "retry_max": 4,
        "retry_backoff_ms": 750,
    }
    defaults = integrations_web._hook_form_defaults(template=template)
    assert defaults["title"] == "n8n Hook"
    assert defaults["url"] == "https://n8n.example/hook"
    assert defaults["retry_max"] == 4
    assert defaults["retry_backoff_ms"] == 750

