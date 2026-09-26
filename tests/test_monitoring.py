from __future__ import annotations

import logging
import sys
import types

from app import monitoring as monitoring_module


class _FakeLokiHandler(logging.Handler):
    def __init__(self, url: str, tags: dict[str, str], version: str):
        super().__init__()
        self.url = url
        self.tags = tags
        self.version = version


class _FakeIntegration:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def test_setup_loki_is_idempotent(monkeypatch):
    fake_module = types.SimpleNamespace(LokiHandler=_FakeLokiHandler)
    monkeypatch.setitem(sys.modules, "logging_loki", fake_module)

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    root_logger.handlers = []
    try:
        assert (
            monitoring_module._setup_loki("dotmac-sub", "srv1", "test", "http://loki")
            is True
        )
        assert (
            monitoring_module._setup_loki("dotmac-sub", "srv1", "test", "http://loki")
            is True
        )
        matching = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, _FakeLokiHandler)
        ]
        assert len(matching) == 1
    finally:
        root_logger.handlers = original_handlers


def test_setup_loki_attaches_the_credential_filter_before_registering(monkeypatch):
    from app.logging import SensitiveQueryFilter

    fake_module = types.SimpleNamespace(LokiHandler=_FakeLokiHandler)
    monkeypatch.setitem(sys.modules, "logging_loki", fake_module)

    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    root_logger.handlers = []
    try:
        assert (
            monitoring_module._setup_loki("dotmac-sub", "srv1", "test", "http://loki")
            is True
        )
        (handler,) = [
            h for h in root_logger.handlers if isinstance(h, _FakeLokiHandler)
        ]
        assert any(isinstance(f, SensitiveQueryFilter) for f in handler.filters)
    finally:
        root_logger.handlers = original_handlers


def test_scrub_event_credentials_redacts_exception_and_logentry_values():
    secret = "hunter2"
    event = {
        "exception": {
            "values": [
                {"type": "RuntimeError", "value": f"failure =password={secret} .tag=1"}
            ]
        },
        "logentry": {
            "message": f"failure =password={secret} .tag=1",
            "formatted": f"failure =password={secret} .tag=1",
            "params": [f"=password={secret} .tag=1", "unrelated"],
        },
        "message": f"failure =password={secret} .tag=1",
    }
    assert secret in event["exception"]["values"][0]["value"]  # sensitivity proof

    scrubbed = monitoring_module._scrub_event_credentials(event, {})

    assert secret not in scrubbed["exception"]["values"][0]["value"]
    assert secret not in scrubbed["logentry"]["message"]
    assert secret not in scrubbed["logentry"]["formatted"]
    assert secret not in scrubbed["logentry"]["params"][0]
    assert scrubbed["logentry"]["params"][1] == "unrelated"
    assert secret not in scrubbed["message"]
    assert "=password=<redacted>" in scrubbed["exception"]["values"][0]["value"]


def test_scrub_event_credentials_is_defensive_about_unexpected_shapes():
    # Must never raise, and must still return an event, regardless of shape.
    assert monitoring_module._scrub_event_credentials({}, {}) == {}
    malformed = {"exception": {"values": "not-a-list"}}
    assert monitoring_module._scrub_event_credentials(malformed, {}) == malformed


def test_scrub_breadcrumb_credentials_redacts_message():
    secret = "hunter2"
    breadcrumb = {"message": f"failure =password={secret} .tag=1"}
    assert secret in breadcrumb["message"]  # sensitivity proof

    scrubbed = monitoring_module._scrub_breadcrumb_credentials(breadcrumb, {})

    assert secret not in scrubbed["message"]
    assert "=password=<redacted>" in scrubbed["message"]


def test_setup_sentry_captures_error_logs(monkeypatch):
    init_calls: list[dict] = []

    sentry_module = types.ModuleType("sentry_sdk")
    sentry_module.init = lambda **kwargs: init_calls.append(kwargs)
    integrations_module = types.ModuleType("sentry_sdk.integrations")
    fastapi_module = types.ModuleType("sentry_sdk.integrations.fastapi")
    sqlalchemy_module = types.ModuleType("sentry_sdk.integrations.sqlalchemy")
    celery_module = types.ModuleType("sentry_sdk.integrations.celery")
    logging_module = types.ModuleType("sentry_sdk.integrations.logging")

    fastapi_module.FastApiIntegration = _FakeIntegration
    sqlalchemy_module.SqlalchemyIntegration = _FakeIntegration
    celery_module.CeleryIntegration = _FakeIntegration
    logging_module.LoggingIntegration = _FakeIntegration

    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", integrations_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.fastapi", fastapi_module)
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk.integrations.sqlalchemy",
        sqlalchemy_module,
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.celery", celery_module)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.logging", logging_module)

    assert (
        monitoring_module._setup_sentry(
            "dotmac-sub",
            "srv1",
            "production",
            "https://glitchtip.example/1",
        )
        is True
    )

    assert len(init_calls) == 1
    logging_integrations = [
        integration
        for integration in init_calls[0]["integrations"]
        if integration.kwargs.get("event_level") == logging.ERROR
    ]
    assert logging_integrations
    assert logging_integrations[0].kwargs["level"] == logging.WARNING
    assert init_calls[0]["before_send"] is monitoring_module._scrub_event_credentials
    assert (
        init_calls[0]["before_breadcrumb"]
        is monitoring_module._scrub_breadcrumb_credentials
    )
