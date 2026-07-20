"""Every registered setting must have a behavior consumer ("no dead controls").

The systemic finding across the UX-polish audits: settings keys that the
generic settings UI (``web_system_settings_views`` iterates every
``SETTINGS_SPECS`` entry) lets an operator edit, but which NO runtime code
reads — so the toggle does nothing. Examples the audits caught: the old FUP
settings page, monitoring ``*_warn_pct``, dead notification prefs.

This lint fails the build when a registered setting key has no reference
anywhere in the codebase outside its own spec definition and the seed. A
"reference" is the key appearing as a quoted string literal in ``app/`` /
``templates/`` / ``scripts/`` — i.e. a ``resolve_value(..., "key")`` call, a
key list passed to ``_read_settings``/``resolve_values_atomic``, or a template
lookup. That is a necessary condition for the setting to affect behavior (not
sufficient — a literal in a hand-written settings page still counts — but it
reliably catches the fully-dead keys with zero plumbing and near-zero false
positives).

The historical orphan backlog was removed in July 2026. A registered key with
no reader now fails immediately; there is deliberately no allowlist.
"""

from __future__ import annotations

import pathlib

from app.services.settings_spec import SETTINGS_SPECS

# Files that define/seed keys are not "readers" — exclude from the corpus.
_EXCLUDED_FILES = {
    "app/services/settings_spec.py",
    "app/services/settings_seed.py",
}


def _repo_root() -> pathlib.Path:
    # tests/architecture/<this file> -> repo root
    return pathlib.Path(__file__).resolve().parents[2]


def _reader_corpus(root: pathlib.Path) -> str:
    chunks: list[str] = []
    for pattern in ("app/**/*.py", "templates/**/*.html", "scripts/**/*.py"):
        for path in root.glob(pattern):
            if str(path.relative_to(root)) in _EXCLUDED_FILES:
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(chunks)


def _find_orphans() -> set[str]:
    corpus = _reader_corpus(_repo_root())
    keys = {spec.key for spec in SETTINGS_SPECS}
    return {k for k in keys if f'"{k}"' not in corpus and f"'{k}'" not in corpus}


def test_no_orphan_settings() -> None:
    orphans = _find_orphans()
    assert not orphans, (
        "Registered setting(s) with no reader (dead control): "
        f"{sorted(orphans)}. Either read the value somewhere it changes "
        "behavior, or drop it from SETTINGS_SPECS."
    )
