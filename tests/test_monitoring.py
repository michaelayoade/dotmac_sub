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
