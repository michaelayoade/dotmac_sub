"""Service package exports."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["contact"]


def __getattr__(name: str) -> Any:
    if name == "contact":
        return importlib.import_module("app.services.contact")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
