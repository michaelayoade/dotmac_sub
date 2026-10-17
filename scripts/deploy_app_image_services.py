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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    try:
        names = app_image_services(json.load(sys.stdin), args.image)
    except (ValueError, json.JSONDecodeError):
        print("compose service graph unavailable", file=sys.stderr)
        return 1
    print("\n".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
