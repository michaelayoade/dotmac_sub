"""Container liveness: the dispatch loop writes a heartbeat file every iteration."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from app.outbox_relay.config import heartbeat_path_from_env


def write_heartbeat(path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"{time.time():.3f}\n", encoding="ascii")
    os.replace(tmp, path)


def heartbeat_is_fresh(path: Path, *, max_age_seconds: float, now: float) -> bool:
    try:
        written = float(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    return 0.0 <= now - written <= max_age_seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-age-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    path = heartbeat_path_from_env(os.environ)
    fresh = heartbeat_is_fresh(
        path, max_age_seconds=args.max_age_seconds, now=time.time()
    )
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
