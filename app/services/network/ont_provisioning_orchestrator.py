"""Deprecated re-exports for backward compatibility.

Use ``app.services.network.ont_provisioning.*`` modules directly.
"""

from app.services.network.ont_provisioning.preflight import validate_prerequisites
from app.services.network.ont_provisioning.preview import preview_commands
from app.services.network.ont_provisioning.profiles import resolve_profile

__all__ = ["preview_commands", "resolve_profile", "validate_prerequisites"]
