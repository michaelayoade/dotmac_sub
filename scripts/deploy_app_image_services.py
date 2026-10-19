"""List the Compose services that run the release app image.

``scripts/deploy.sh`` recreates exactly these services on every deploy. The
set is derived from the resolved Compose config instead of a hand-kept list,
because a hand-kept list silently drifted: ``team-inbox-smtp`` runs the app
image but was never recreated, and stayed on an old image for a month.

Resolved Compose JSON contains host secrets. It is consumed in memory and only
service names are printed; nothing from the document ever reaches an error.

INTERIM: per-service desired state and drift belong to Starter
dotmac-deployment-control / dotmac-release-catalog; this goes away with them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence


def app_image_services(document: object, image: str) -> list[str]:
    """Return service names whose resolved image is exactly ``image``."""
    if not image or not isinstance(document, dict):
        raise ValueError("compose document or image missing")
    services = document.get("services")
    if not isinstance(services, dict):
        raise ValueError("compose document has no services")
    return sorted(
        name
        for name, service in services.items()
        if isinstance(service, dict) and service.get("image") == image
    )


#: App-image services a deploy may CREATE when no container exists yet. Any
#: other app-image service in the resolved config (for example an `app-backup`
#: declared only in a host override) is recreated only if the host already runs
#: it: a deploy must never create a service the host has never run.
REQUIRED_APP_SERVICES: tuple[str, ...] = (
    "app",
    "celery-worker",
    "celery-worker-bandwidth",
    "celery-worker-ingestion",
    "celery-worker-monitoring",
    "celery-worker-notifications-immediate",
    "celery-worker-notifications",
    "celery-worker-billing",
    "celery-worker-tr069",
    "celery-beat",
    "bandwidth-poller",
    "syslog-listener",
    "team-inbox-smtp",
)


def recreate_set(
    declared_app_image: Sequence[str],
    existing: Sequence[str],
    excluded: Sequence[str] = (),
    required: Sequence[str] = REQUIRED_APP_SERVICES,
) -> list[str]:
    """Services a deploy recreates.

    ``declared_app_image``: resolved-config services on the release image.
    ``existing``: services with a non-one-off project container on a
    dotmac_sub app image (a profile-gated service started by name is here even
    though its profile is inactive, so config does not list it).
    """
    allowed = set(required) | set(existing)
    chosen = {name for name in declared_app_image if name in allowed}
    chosen |= set(existing)
    return sorted(chosen - set(excluded))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--existing", action="append", default=[])
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        declared = app_image_services(json.load(sys.stdin), args.image)
    except (ValueError, json.JSONDecodeError):
        print("compose service graph unavailable", file=sys.stderr)
        return 1
    print("\n".join(recreate_set(declared, args.existing, args.exclude)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
