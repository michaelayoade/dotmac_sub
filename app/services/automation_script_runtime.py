"""Deployment-owned readiness contract for server-script execution.

This module intentionally contains no JavaScript or Python evaluator. The
runtime is an external OCI image behind the existing hardened Podman
transport. Until an image and sha256 digest are configured, server-script
publication must remain unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.config import settings

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class AutomationScriptRuntimeState(StrEnum):
    ready = "ready"
    unavailable = "unavailable"
    invalid = "invalid"


@dataclass(frozen=True, slots=True)
class AutomationScriptRuntimePolicy:
    image: str
    digest: str
    timeout_seconds: int
    network: str = "none"

    @property
    def image_ref(self) -> str:
        return f"{self.image}@{self.digest}"


def current_policy() -> AutomationScriptRuntimePolicy | None:
    if (
        not settings.automation_script_runtime_image
        or not settings.automation_script_runtime_digest
    ):
        return None
    return AutomationScriptRuntimePolicy(
        image=settings.automation_script_runtime_image,
        digest=settings.automation_script_runtime_digest,
        timeout_seconds=settings.automation_script_runtime_timeout_seconds,
    )


def runtime_state() -> AutomationScriptRuntimeState:
    if (
        not settings.automation_script_runtime_image
        or not settings.automation_script_runtime_digest
    ):
        return AutomationScriptRuntimeState.unavailable
    if not _DIGEST.fullmatch(settings.automation_script_runtime_digest):
        return AutomationScriptRuntimeState.invalid
    return AutomationScriptRuntimeState.ready


__all__ = [
    "AutomationScriptRuntimePolicy",
    "AutomationScriptRuntimeState",
    "current_policy",
    "runtime_state",
]
