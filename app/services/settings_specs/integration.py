"""Integration-domain settings.

Connector endpoints, credentials, enablement, and retry policy live in versioned
integration installation config revisions. This module remains as the stable
settings-registry extension point and intentionally declares no connector
configuration keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def build_integration_specs(_setting_spec: Callable[..., Any]) -> list[Any]:
    return []
