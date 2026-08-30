"""The CRM ticket poller is gated by one database row and nothing else.

On 2026-08-30 two careful readers spent a day reasoning about what was
*enabling* `crm.ticket_pull`, because production carried
`CRM_TICKET_PULL_ENABLED=true` in its environment. Nothing was enabling it.
The canonical row `modules.crm_ticket_pull` had been Off since 2026-08-18, and
the environment variable is retired residue that no code reads.

Both of us established correctly which input governs. Neither of us read what
it said. The variable cost a day precisely because it *looked* like an input,
and it will look like one again to the next reader.

So this pins the property that makes the residue harmless: the schedule is
gated by `control_registry.is_enabled("crm.ticket_pull")`, that resolution
reads a `domain_settings` row and the registry default, and **no path from the
environment exists**. If someone adds one, the residue stops being harmless
and this fails.

Scoped to the resolution path, deliberately. It asserts nothing about whether
the control is on or off — that is production state, and a test that claimed
to know it would be lying.
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEDULER = Path("app/services/scheduler_config.py")
REGISTRY = Path("app/services/control_registry.py")
APP = Path("app")

CONTROL_KEY = "crm.ticket_pull"
RETIRED_ENV_VAR = "CRM_TICKET_PULL_ENABLED"
RETIRED_SETTING_KEY = "crm_ticket_pull_enabled"

#: The two places the retired names may still be spoken: the registry's own
#: `LegacyAlias` record that they ARE retired, and the orphaned scheduler spec
#: that the owning slice will remove. Both are declarations; neither is a read.
DECLARATION_SITES = {
    "app/services/control_registry.py",
    "app/services/settings_spec.py",
}

_ENV_READ = re.compile(r"os\.getenv\s*\(|os\.environ")


def _python_sources(root: Path) -> list[tuple[str, str]]:
    return [
        (path.as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def env_reads_naming(source: str, name: str) -> list[str]:
    """Lines that read the environment AND mention `name`."""

    return [
        line.strip()
        for line in source.splitlines()
        if name in line and _ENV_READ.search(line)
    ]


def quoted_key_uses(source: str, key: str) -> list[str]:
    """Lines using `key` as a STRING literal.

    Quoted is the discriminator that matters. `scheduler_config` binds a local
    named `crm_ticket_pull_enabled` from `is_enabled(...)` — that is the
    canonical path wearing the old name, not a resolution of the retired key.
    Only a quoted occurrence can address a settings row.
    """

    return [
        line.strip()
        for line in source.splitlines()
        if f'"{key}"' in line or f"'{key}'" in line
    ]


# ── the resolution path ──────────────────────────────────────────────────────


def test_the_schedule_is_gated_by_the_canonical_control() -> None:
    source = SCHEDULER.read_text(encoding="utf-8")
    assert f'"{CONTROL_KEY}"' in source and "control_registry.is_enabled" in source, (
        "the CRM schedule no longer resolves through "
        f"control_registry.is_enabled({CONTROL_KEY!r}); if the gate moved, this "
        "guard must move with it or it is proving nothing"
    )
    for key in ("crm_ticket_pull", "crm_ticket_pull_full"):
        assert f'schedule["{key}"]' in source, (
            f"the {key} beat entry is gone from scheduler_config. If the slice "
            "that owns it removed it, delete this assertion in the same change"
        )


def test_control_resolution_cannot_reach_the_environment() -> None:
    """The registry resolves a row and a default. It has no env path at all."""

    offending = [
        line
        for line in REGISTRY.read_text(encoding="utf-8").splitlines()
        if _ENV_READ.search(line)
    ]
    assert not offending, (
        "app/services/control_registry.py now reads the environment. Control "
        "resolution is a database row plus the registry default; an "
        "environment path would make retired aliases live again: "
        f"{offending}"
    )


def test_nothing_reads_the_retired_environment_variable() -> None:
    found = {
        path: hits
        for path, source in _python_sources(APP)
        if (hits := env_reads_naming(source, RETIRED_ENV_VAR))
    }
    assert not found, (
        f"{RETIRED_ENV_VAR} is retired residue — production still carries it "
        "and it misled two readers for a day. It is harmless only while "
        f"nothing reads it: {found}"
    )


def test_the_retired_setting_key_is_only_declared_never_resolved() -> None:
    found = {
        path: hits
        for path, source in _python_sources(APP)
        if path not in DECLARATION_SITES
        and (hits := quoted_key_uses(source, RETIRED_SETTING_KEY))
    }
    assert not found, (
        f"the retired scheduler alias {RETIRED_SETTING_KEY!r} is referenced "
        "outside its two declaration sites. Migration 309 materialised it into "
        "modules.crm_ticket_pull and deleted the row; a new reader would be "
        f"resolving a key that no longer exists: {found}"
    )


# ── sensitivity proof ────────────────────────────────────────────────────────


def test_the_detectors_fire_on_the_shapes_they_forbid() -> None:
    """Every assertion above passes over an empty set today.

    Without this, all four would keep passing if the detectors stopped
    matching — which is the failure mode a guard written after the incident is
    most likely to have.
    """

    env_shapes = "\n".join(
        (
            f'enabled = os.getenv("{RETIRED_ENV_VAR}", "false")',
            f'flag = os.environ["{RETIRED_ENV_VAR}"]',
            f'raw = os.environ.get("{RETIRED_ENV_VAR}")',
        )
    )
    assert len(env_reads_naming(env_shapes, RETIRED_ENV_VAR)) == 3, (
        "the environment-read detector missed a shape it must catch: "
        f"{env_reads_naming(env_shapes, RETIRED_ENV_VAR)}"
    )

    setting_shape = (
        f'value = resolve_value(db, SettingDomain.scheduler, "{RETIRED_SETTING_KEY}")'
    )
    assert quoted_key_uses(setting_shape, RETIRED_SETTING_KEY), (
        "the retired-setting detector missed a resolve_value call"
    )
    local_binding = f"    {RETIRED_SETTING_KEY} = control_registry.is_enabled(db, key)"
    assert not quoted_key_uses(local_binding, RETIRED_SETTING_KEY), (
        "the detector flagged the canonical path's local variable, which would "
        "make it fire on correct code and get it deleted"
    )


def test_the_environment_detector_does_not_fire_on_a_mere_mention() -> None:
    """Specificity: naming the variable in prose is not reading it.

    Both declaration sites and every runbook mention it by name. A detector
    that flagged those would be deleted within a week.
    """

    prose = "\n".join(
        (
            f'LegacyAlias(_SCH, "{RETIRED_SETTING_KEY}", "{RETIRED_ENV_VAR}"),',
            f"# {RETIRED_ENV_VAR} is retired residue and nothing reads it.",
            f'env_var="{RETIRED_ENV_VAR}",',
        )
    )
    assert not env_reads_naming(prose, RETIRED_ENV_VAR), (
        "the detector flagged a declaration or a comment as an environment read"
    )
