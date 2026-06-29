"""IntegrationHook auth_config secrets are encrypted at rest, decrypted on use."""

from __future__ import annotations

from app.services import integration_hooks as hooks_service


def _make_hook(db, *, auth_type, auth_config):
    return hooks_service.create_hook(
        db,
        title="enc-hook",
        hook_type="web",
        command=None,
        url="https://example.test/hook",
        http_method="POST",
        auth_type=auth_type,
        auth_config=auth_config,
        retry_max=1,
        retry_backoff_ms=1,
        event_filters=None,
        is_enabled=True,
        notes=None,
    )


def test_secret_values_encrypted_non_secret_kept(db_session):
    hook = _make_hook(
        db_session,
        auth_type="basic",
        auth_config={"username": "u", "password": "p@ss", "secret": "sk"},
    )
    stored = hook.auth_config
    # secrets are wrapped at rest...
    assert stored["password"] != "p@ss"
    assert stored["password"].startswith(("enc:", "plain:"))
    assert stored["secret"].startswith(("enc:", "plain:"))
    # ...non-secret keys are untouched...
    assert stored["username"] == "u"
    # ...and the use-path round-trips to plaintext.
    assert hooks_service._decrypt_auth_secret(stored, "password") == "p@ss"
    assert hooks_service._decrypt_auth_secret(stored, "secret") == "sk"


def test_execute_sends_decrypted_bearer_token(db_session, monkeypatch):
    hook = _make_hook(
        db_session, auth_type="bearer", auth_config={"token": "abc123"}
    )
    assert hook.auth_config["token"] != "abc123"  # encrypted at rest

    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_request(*, method, url, headers, json, timeout):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(hooks_service.httpx, "request", _fake_request)
    hooks_service._execute_http_hook(hook=hook, payload={})

    # The wire value is the decrypted secret, never the ciphertext.
    assert captured["headers"]["Authorization"] == "Bearer abc123"


def test_decrypt_handles_legacy_plaintext(db_session):
    # A hook stored before the change keeps plaintext values; the use-path must
    # still read them (decrypt_credential treats unprefixed values as plaintext).
    assert hooks_service._decrypt_auth_secret({"token": "legacy"}, "token") == "legacy"
