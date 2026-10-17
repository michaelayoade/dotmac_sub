#!/usr/bin/env python3
"""Validate a version-bump patch before the write-scoped job applies it.

The bump job runs third-party code, so its patch is untrusted data. This gate
parses the unified diff itself (stdlib only, no git, no repo imports) and
accepts it only if every file and every changed line is one that
``scripts/bump_version.py`` legitimately writes:

* only whole-path matches against ``ALLOWED_PATHS``; no creates, deletes,
  renames, mode changes, symlinks, binary hunks or quoted/odd paths;
* ``VERSION`` is exactly ``old -> NEW`` with NEW strictly greater than old
  (and old equal to the base tree's VERSION when ``--root`` is given);
* every other changed line is a version-string line (per-file regex), the
  dependency-manifest digest lines, ``manifest_digest`` in deploy/product.toml,
  the ``product_version`` field of deploy/product-manifest.json (the only
  version-bearing field emit_product_manifest writes; the manifest has no
  digest field), or a CHANGELOG entry heading.

Exit status 0 means valid; any problem prints one line to stderr and exits 1.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
HEX64 = r"[0-9a-f]{64}"

ALLOWED_PATHS = frozenset(
    {
        "VERSION",
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        "mobile/pubspec.yaml",
        "mobile/lib/src/config/env.dart",
        "mobile/lib/main.dart",
        "CHANGELOG.md",
        "docs/kernel-runtime-composition.json",
        "deploy/product-manifest.json",
        "deploy/product.toml",
    }
)

# Version-line templates; {V} is the escaped old or new version.
VERSION_LINES = {
    "pyproject.toml": r'version = "{V}"',
    "package.json": r'  "version": "{V}",?',
    "package-lock.json": r'\s*"version": "{V}",?',
    "mobile/lib/src/config/env.dart": r"\s*String\.fromEnvironment\('APP_VERSION', defaultValue: '{V}'\);",
    "mobile/lib/main.dart": r"\s*options\.release = 'dotmac-mobile@{V}';",
}
DIGEST_LINES = {
    "docs/kernel-runtime-composition.json": [
        rf'\s*"source_blob_sha256": "{HEX64}",?',
        rf'\s*"extract_sha256": "{HEX64}"',
    ],
    "deploy/product.toml": [rf'manifest_digest = "sha256:{HEX64}"'],
}
MANIFEST_FIELD = '"product_version":"{V}"'
HUNK = re.compile(r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@")
INDEX = re.compile(r"^index [0-9a-f]{7,64}\.\.[0-9a-f]{7,64} 100644$")


class PatchError(Exception):
    pass


def _parts(version: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(version)
    if not match:
        raise PatchError(f"malformed version {version!r}")
    return tuple(int(piece) for piece in match.groups())  # type: ignore[return-value]


def _split(patch: bytes) -> list[tuple[str, list[str], list[str]]]:
    """Return (path, removed, added) per file, rejecting anything unexpected."""
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError("patch is not valid UTF-8 text") from exc
    if not text.strip():
        raise PatchError("empty patch")
    if "\x00" in text or "\r" in text:
        raise PatchError("patch contains NUL or CR")
    if not text.endswith("\n"):
        raise PatchError("patch does not end with a newline")

    files: list[tuple[str, list[str], list[str]]] = []
    seen: set[str] = set()
    state = "start"  # start | index | minus | plus | hunk
    path = ""
    removed: list[str] = []
    added: list[str] = []
    for line in text[:-1].split("\n"):
        if state in ("start", "hunk") and line.startswith("diff --git "):
            if state == "hunk":
                files.append((path, removed, added))
            elif state != "start":
                raise PatchError("truncated file header")
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not match or match.group(1) != match.group(2):
                raise PatchError("diff header mismatch or unsupported header")
            path = match.group(1)
            if not SAFE_PATH.fullmatch(path) or ".." in path.split("/"):
                raise PatchError(f"unsafe path {path!r}")
            if path not in ALLOWED_PATHS:
                raise PatchError(f"path not allowlisted: {path}")
            if path in seen:
                raise PatchError(f"duplicate file block: {path}")
            seen.add(path)
            removed, added = [], []
            state = "index"
        elif state == "index":
            if not INDEX.fullmatch(line):
                raise PatchError(f"unsupported header line for {path}: {line[:60]!r}")
            state = "minus"
        elif state == "minus":
            if line != f"--- a/{path}":
                raise PatchError(f"---/header mismatch for {path}")
            state = "plus"
        elif state == "plus":
            if line != f"+++ b/{path}":
                raise PatchError(f"+++/header mismatch for {path}")
            state = "hunkhead"
        elif state in ("hunkhead", "hunk"):
            if HUNK.match(line):
                state = "hunk"
            elif state == "hunk" and line == "\\ No newline at end of file":
                continue
            elif state == "hunk" and line[:1] in ("-", "+", " "):
                if line[0] == "-":
                    removed.append(line[1:])
                elif line[0] == "+":
                    added.append(line[1:])
            else:
                raise PatchError(f"unexpected line in {path}: {line[:60]!r}")
        else:
            raise PatchError(f"unexpected line: {line[:60]!r}")
    if state != "hunk":
        raise PatchError("truncated or garbage patch")
    files.append((path, removed, added))
    return files


def _check_lines(
    path: str, removed: list[str], added: list[str], old: str, new: str
) -> None:
    eo, en = re.escape(old), re.escape(new)
    if path == "VERSION":
        if removed != [old] or added != [new]:
            raise PatchError(f"VERSION must change exactly {old} -> {new}")
    elif path in VERSION_LINES:
        template = VERSION_LINES[path]
        if not removed or len(removed) != len(added):
            raise PatchError(f"{path}: unbalanced changes")
        for r, a in zip(removed, added, strict=True):
            if not re.fullmatch(template.replace("{V}", eo), r) or not re.fullmatch(
                template.replace("{V}", en), a
            ):
                raise PatchError(f"{path}: non-version change: {a[:60]!r}")
    elif path == "mobile/pubspec.yaml":
        if len(removed) != 1 or len(added) != 1:
            raise PatchError(f"{path}: unexpected change count")
        r = re.fullmatch(rf"version: {eo}\+(\d+)", removed[0])
        a = re.fullmatch(rf"version: {en}\+(\d+)", added[0])
        if not r or not a or int(a.group(1)) != int(r.group(1)) + 1:
            raise PatchError(f"{path}: not a version/build line bump")
    elif path == "CHANGELOG.md":
        if removed:
            raise PatchError("CHANGELOG.md: removals are not allowed")
        ok = (
            rf"## {en} - \d{{4}}-\d{{2}}-\d{{2}}",
            "",
            "- Version bump.",
        )
        if not added or not all(
            any(re.fullmatch(p, line) for p in ok) for line in added
        ):
            raise PatchError("CHANGELOG.md: only a version-bump entry is allowed")
    elif path in DIGEST_LINES:
        if not removed or len(removed) != len(added):
            raise PatchError(f"{path}: unbalanced changes")
        for line in removed + added:
            if not any(re.fullmatch(p, line) for p in DIGEST_LINES[path]):
                raise PatchError(f"{path}: non-digest change: {line[:60]!r}")
    elif path == "deploy/product-manifest.json":
        if len(removed) != 1 or len(added) != 1:
            raise PatchError(f"{path}: unexpected change count")
        before = MANIFEST_FIELD.replace("{V}", old)
        after = MANIFEST_FIELD.replace("{V}", new)
        if (
            removed[0].count(before) != 1
            or removed[0].replace(before, after) != added[0]
        ):
            raise PatchError(f"{path}: only product_version may change")
    else:  # pragma: no cover - guarded by the allowlist
        raise PatchError(f"no rule for {path}")


def validate(patch: bytes, version: str, root: Path | None = None) -> None:
    new = version
    _parts(new)
    files = _split(patch)
    by_path = {path: (rem, add) for path, rem, add in files}
    if "VERSION" not in by_path or len(by_path["VERSION"][0]) != 1:
        raise PatchError("patch must change VERSION")
    old = by_path["VERSION"][0][0]
    if _parts(new) <= _parts(old):
        raise PatchError(f"version {new} is not greater than {old}")
    if root is not None:
        base = (root / "VERSION").read_text(encoding="utf-8")
        if base != f"{old}\n":
            raise PatchError("patch VERSION does not match the base tree")
    for path, removed, added in files:
        _check_lines(path, removed, added, old, new)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patch", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    try:
        validate(args.patch.read_bytes(), args.version, args.root)
    except (PatchError, OSError) as exc:
        print(f"bump patch rejected: {exc}", file=sys.stderr)
        return 1
    print("bump patch ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
