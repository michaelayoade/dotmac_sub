"""Shared analysis for the CRM/Omni vocabulary freeze.

The `dotmac_crm` deployment ("Omni") was decommissioned on 2026-08-29. Its
surface inside Sub is being replaced domain by domain — Inbox/Chat, then
Support/Ticketing, Sales/Quotes, Party/Customer/Reseller, ERP modules, then a
final secrets/deployment/observability pass — each slice deleting residue
BESIDE its replacement owner rather than in a sweep.

A slice-by-slice replacement only works if the surface holds still between
slices. This freeze is what holds it: it fails when a NEW CRM dependency lands,
and it fails when one disappears without the baseline being lowered in the same
change. A silent removal is as damaging as a silent addition here, because the
whole programme is a sequence of deliberate, recorded removals.

## Why the match is on identifier FORM, not on the bare word

`\\b` is the trap. A word boundary does not fire beside `_` or a digit, so a
naive `\\bcrm\\b` reads `crm_subscriber_id`, `crm_ticket_pull` and `CRMClient`
as clean — which is nearly the entire real surface. This module instead splits
camelCase into `_`, splits on every non-alphanumeric run, lowercases, and
compares whole TOKENS against the frozen vocabulary.

Token equality also buys specificity for free: a base64 digest or a long opaque
string is one token and never equals `crm`, so it cannot false-positive the way
a substring search would.

## What counts as a member

A file is in the surface if the vocabulary appears in its CONTENT **or in its
PATH**. The path half is not decorative: `app/services/reseller_crm_views.py`
is named for the CRM and contains no `crm` token in its body, so a
content-only scan misses a file whose whole subject is the CRM.

Tests that assert a CRM/Omni alias is *refused* are members too — for example
the ERP contract tests pinning the retired `omni_id`. They reference the
vocabulary, and deleting one silently is exactly what the falling direction of
this ratchet exists to catch.
"""

from __future__ import annotations

import re
import subprocess
from functools import cache
from pathlib import Path

#: The frozen vocabulary. `omni` is here because the deployment was named Omni
#: and its identifiers outlived the CRM name in several contracts.
CRM_TERMS: frozenset[str] = frozenset({"crm", "omni"})

#: Entry-point families. Application code with its Celery tasks and workers
#: (`app`), schema migrations (`alembic`), operator CLI, seeds and one-offs
#: (`scripts`), the test suite, and the documentation that carries the
#: programme's contracts.
LANES: tuple[str, ...] = ("app", "alembic", "docs", "scripts", "tests")

# Split `fooBar` and `CRMClient` alike: the second alternative is what turns
# `CRMClient` into `CRM_Client` rather than leaving one opaque token.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def tokens(text: str) -> frozenset[str]:
    """Lowercase identifier tokens, with camelCase treated as a separator."""

    split = _CAMEL_BOUNDARY.sub("_", text)
    return frozenset(part.lower() for part in _NON_ALNUM.split(split) if part)


def mentions_crm(text: str) -> bool:
    """Whether a fragment carries the frozen vocabulary as a whole token."""

    return bool(tokens(text) & CRM_TERMS)


@cache
def tracked_files() -> tuple[str, ...]:
    """Every tracked path, from git.

    Tracked rather than on-disk on purpose: a freeze is a statement about the
    repository, and an untracked scratch file in `docs/` must not be able to
    trip it.
    """

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(sorted(path for path in result.stdout.split("\0") if path))


def _reads_as_text(path: Path) -> str | None:
    """Decode a tracked file, or None if it is binary or unreadable.

    The binary check is load-bearing. `docs/My Map.kmz` is a zip archive whose
    bytes, forced through a lossy UTF-8 decode, happen to yield a `crm` token
    and put a map file in the CRM surface. A scanner that decodes binary does
    not find things; it invents them.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="ignore")


@cache
def surface_by_lane() -> dict[str, frozenset[str]]:
    """Files carrying the CRM/Omni vocabulary, grouped by entry-point family."""

    found: dict[str, set[str]] = {lane: set() for lane in LANES}
    for tracked in tracked_files():
        lane, _, _ = tracked.partition("/")
        if lane not in found:
            continue
        if mentions_crm(tracked):
            found[lane].add(tracked)
            continue
        text = _reads_as_text(Path(tracked))
        if text is not None and any(mentions_crm(line) for line in text.splitlines()):
            found[lane].add(tracked)
    return {lane: frozenset(paths) for lane, paths in found.items()}


def surface_paths() -> frozenset[str]:
    """The whole frozen surface as one set."""

    return frozenset().union(*surface_by_lane().values())
