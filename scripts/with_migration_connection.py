#!/usr/bin/env python3
"""Exec one declared deploy command with a held migration connection.

The host's authorized materializer owns the file. This adapter reads it once,
checks its local authority, and never prints or persists the URL.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_URL_BYTES = 8192
MAX_POINTER_CHARS = 4096
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class LoaderRefusal(ValueError):
    """A bounded, non-secret refusal code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _protected_roots(environ: Mapping[str, str]) -> tuple[Path, ...]:
    roots = [Path.cwd(), Path(__file__).resolve().parents[1]]
    for name in (
        "GITHUB_WORKSPACE",
        "STAGING_DEPLOY_DIR",
        "PRODUCTION_DEPLOY_DIR",
        "DEPLOY_DIR",
        "REPO_DIR",
    ):
        value = environ.get(name)
        if value:
            roots.append(Path(value).resolve(strict=False))
    return tuple(root.resolve(strict=False) for root in roots)


def _pointer(environ: Mapping[str, str]) -> Path:
    raw = environ.get("MIGRATION_DATABASE_URL_FILE")
    if (
        raw is None
        or not raw
        or len(raw) > MAX_POINTER_CHARS
        or raw != raw.strip()
        or any(char in raw for char in ("\x00", "\n", "\r"))
        or not os.path.isabs(raw)
    ):
        raise LoaderRefusal("invalid_pointer")
    path = Path(raw)
    try:
        resolved = path.resolve(strict=False)
        protected_roots = _protected_roots(environ)
    except (OSError, RuntimeError, ValueError) as error:
        raise LoaderRefusal("invalid_pointer") from error
    if any(resolved.is_relative_to(root) for root in protected_roots):
        raise LoaderRefusal("source_or_deploy_path")
    return path


def _validated_url(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 for character in value)
        or _BAD_PERCENT_ESCAPE.search(value)
    ):
        raise LoaderRefusal("invalid_url")
    try:
        parsed = urlsplit(value)
        username = parsed.username
        hostname = parsed.hostname
        _ = parsed.port
        database = unquote(parsed.path.removeprefix("/"))
    except ValueError as error:
        raise LoaderRefusal("invalid_url") from error
    if (
        parsed.scheme not in {"postgresql", "postgresql+psycopg"}
        or username != "app_admin"
        or not hostname
        or not database
        or "/" in database
        or parsed.fragment
        or not parsed.netloc
    ):
        raise LoaderRefusal("invalid_url")
    return value


def load_migration_url(environ: Mapping[str, str]) -> str:
    """Return a URL only after file, location, and principal validation."""

    if "MIGRATION_DATABASE_URL" in environ:
        raise LoaderRefusal("existing_url_conflict")
    if "DATABASE_URL" in environ:
        raise LoaderRefusal("inherited_runtime_url")
    path = _pointer(environ)
    # A FIFO opened read-only without O_NONBLOCK waits for a writer before
    # fstat can reject it. The source is a regular file, so nonblocking open
    # has no effect on the accepted path.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise LoaderRefusal("unreadable_file") from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_size <= 0
                or before.st_size > MAX_URL_BYTES
            ):
                raise LoaderRefusal("invalid_file_authority")
            data = stream.read(MAX_URL_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError as error:
        raise LoaderRefusal("unreadable_file") from error
    if (
        len(data) != before.st_size
        or len(data) > MAX_URL_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise LoaderRefusal("changed_or_oversized_file")
    try:
        value = data.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise LoaderRefusal("invalid_url") from error
    return _validated_url(value)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--" or len(args) < 2:
        print("migration connection loader refused: missing_command", file=sys.stderr)
        return 2
    try:
        url = load_migration_url(os.environ)
    except LoaderRefusal as error:
        print(f"migration connection loader refused: {error.code}", file=sys.stderr)
        return 2
    child_env = dict(os.environ)
    child_env.pop("MIGRATION_DATABASE_URL_FILE", None)
    child_env["MIGRATION_DATABASE_URL"] = url
    try:
        os.execvpe(args[1], args[1:], child_env)  # noqa: S606 - no shell is intended
    except OSError:
        print(
            "migration connection loader refused: command_exec_failed", file=sys.stderr
        )
        return 2
    raise AssertionError("os.execvpe returned without replacing the process")


if __name__ == "__main__":
    raise SystemExit(main())
