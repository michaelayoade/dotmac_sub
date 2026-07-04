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

``_KNOWN_ORPHAN_SETTINGS`` is a burn-down backlog of pre-existing dead keys,
captured 2026-07-03. Each should eventually be either wired to a consumer or
removed from ``SETTINGS_SPECS``. Do NOT add to it: a newly-registered key with
no reader fails immediately, which is the whole point.
"""

from __future__ import annotations

import pathlib

from app.services.settings_spec import SETTINGS_SPECS

# Files that define/seed keys are not "readers" — exclude from the corpus.
_EXCLUDED_FILES = {
    "app/services/settings_spec.py",
    "app/services/settings_seed.py",
}

# Pre-existing registered-but-unread keys (2026-07-03). Burn-down only — wire or
# remove each, never extend. A new orphan must fail the build instead.
_KNOWN_ORPHAN_SETTINGS: set[str] = {
    "account_number_enabled",
    "account_number_padding",
    "account_number_start",
    "core_device_ping_interval_seconds",
    "core_device_snmp_walk_interval_seconds",
    "default_account_status",
    "default_contact_role",
    "default_material_status",
    "default_olt_port_type",
    "default_reservation_status",
    "default_splitter_input_ports",
    "default_splitter_output_ports",
    "hotspot_redirect_url",
    "hotspot_walled_garden",
    "meta_access_token_override",
    "meta_api_timeout_seconds",
    "meta_oauth_redirect_uri",
    "meta_webhook_verify_token",
    "notification_category_preferences_enabled",
    "olt_polling_interval_minutes",
    "ont_offline_poll_threshold",
    "pon_outage_min_offline_onus",
    # prepaid_* enforcement settings wired 2026-07-04 by the prepaid balance
    # sweep (app/services/collections/prepaid_balance_sweep.py).
    "vendor_bid_minimum_days",
    "vendor_quote_approval_threshold",
    "vendor_quote_validity_days",
    "vendor_remember_ttl_seconds",
    "vendor_session_ttl_seconds",
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


def test_no_new_orphan_settings() -> None:
    orphans = _find_orphans()
    new_orphans = orphans - _KNOWN_ORPHAN_SETTINGS
    assert not new_orphans, (
        "Registered setting(s) with no reader (dead control): "
        f"{sorted(new_orphans)}. Either read the value somewhere it changes "
        "behavior, or drop it from SETTINGS_SPECS. Do not add to "
        "_KNOWN_ORPHAN_SETTINGS."
    )


def test_known_orphan_list_is_accurate() -> None:
    # Keeps the burn-down honest: once a known orphan is wired or removed, it
    # must be deleted from the allowlist so the list shrinks toward empty.
    orphans = _find_orphans()
    stale = _KNOWN_ORPHAN_SETTINGS - orphans
    assert not stale, (
        "These keys are no longer orphaned (wired or removed) — delete them "
        f"from _KNOWN_ORPHAN_SETTINGS: {sorted(stale)}"
    )
