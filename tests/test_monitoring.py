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
