"""Deprecated — re-exports from ont_provision_steps for backward compatibility.

All functions have moved to ``app.services.network.ont_provision_steps``.
Update imports to point there directly.
"""

from app.services.network.ont_provision_steps import (  # noqa: F401
    preview_commands,
    resolve_profile,
    validate_prerequisites,
)

__all__ = ["preview_commands", "resolve_profile", "validate_prerequisites"]
