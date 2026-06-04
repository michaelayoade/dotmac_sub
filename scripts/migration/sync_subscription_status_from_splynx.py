"""Compat shim — actual code lives in app.services.migrations.sync_subscription_status_from_splynx.

Keeps CLI working: `python -m scripts.migration.sync_subscription_status_from_splynx`
"""

from app.services.migrations.sync_subscription_status_from_splynx import *  # noqa: F401,F403
from app.services.migrations.sync_subscription_status_from_splynx import (
    run,  # noqa: F401
)

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    run(dry_run=not args.execute)
