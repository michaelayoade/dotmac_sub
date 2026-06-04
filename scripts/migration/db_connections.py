"""Compat shim — actual code lives in app.services.migrations.db_connections.

Kept here so scripts/migration/*.py modules that haven't been moved yet still
work via their existing imports.
"""

from app.services.migrations.db_connections import *  # noqa: F401,F403
from app.services.migrations.db_connections import (  # noqa: F401
    dotmac_session,
    fetch_all,
    fetch_batched,
    splynx_connection,
)
