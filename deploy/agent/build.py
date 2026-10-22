"""Build the deterministic source zipapp; installation never self-updates."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        files = {
            f"deploy/agent/{path.name}": path.read_bytes()
            for path in sorted(source.glob("*.py"))
            if path.name not in {"build.py", "provision.py"}
        }
        files["__main__.py"] = b"from deploy.agent.agent import main\nmain()\n"
        files["deploy/__init__.py"] = b""
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)


if __name__ == "__main__":
    main()
