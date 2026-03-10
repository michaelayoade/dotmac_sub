"""Settings API compatibility module.

Re-exports settings API helpers from focused submodules.
"""

from app.services.settings_api_custom import *  # noqa: F403
from app.services.settings_api_generic import *  # noqa: F403

__all__ = [name for name in globals() if not name.startswith("_")]
