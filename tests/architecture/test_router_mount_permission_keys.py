"""Router-mount permission modes must derive keys that are actually seeded.

``app.main._router_dependencies`` BUILDS its requirement keys from the mount
mode with f-strings: ``"perm:<domain>"`` becomes
``require_method_permission(f"{domain}:read", f"{domain}:write")`` and
``"readperm:<key>"`` becomes ``require_permission(key)``. Because those strings
never appear as literals in ``app/``, ``test_rbac_seed_parity`` — which walks
the tree for literal guard arguments — cannot see them, and
``test_route_permission_guards`` only checks that *a* guard callable is
attached, not that its key exists.

A mode typo therefore ships silently and then fails CLOSED at runtime:
``require_permission`` raises 403 for an unknown key, so the whole router
answers 403 to every non-admin principal while admins sail through on the
``is_admin`` short-circuit and hide it.

That is exactly what ``("app.api.dispatch", ..., "perm:operations:dispatch:read")``
did — the ``perm:`` mode appends to the DOMAIN, so it derived
``operations:dispatch:read:read`` and ``operations:dispatch:read:write``, and
neither is in ``DEFAULT_PERMISSIONS`` (the seeded keys are
``operations:dispatch:read`` / ``:write`` / ``:assign``). The whole
``/api/v1/dispatch`` surface was unreachable for non-admins.

This guard works from the MOUNT TABLE, not from literals, so it sees the built
keys.
"""

from __future__ import annotations

import inspect
import re

from app.main import (
    _CORE_ROUTER_SPECS,
    _DEFERRED_API_ROUTER_SPECS,
    _router_dependencies,
)
from scripts.seed.seed_rbac import DEFAULT_PERMISSIONS

RouterSpec = tuple[str, str, str, str]

# Mount modes that resolve to a permission key. Kept in sync with
# ``_router_dependencies`` by ``test_permission_mount_prefixes_are_covered``.
_PERM_PREFIX = "perm:"
_READPERM_PREFIX = "readperm:"


def _seeded_keys() -> set[str]:
    return {key for key, _description in DEFAULT_PERMISSIONS}


def _derived_permission_keys(mode: str) -> tuple[str, ...]:
    """The permission keys ``_router_dependencies`` would require for ``mode``."""
    if mode.startswith(_PERM_PREFIX):
        domain = mode[len(_PERM_PREFIX) :]
        return (f"{domain}:read", f"{domain}:write")
    if mode.startswith(_READPERM_PREFIX):
        return (mode[len(_READPERM_PREFIX) :],)
    return ()


def _unseeded_mount_keys(specs: list[RouterSpec]) -> list[str]:
    """Report every ``<module>:<attr> mode=<mode> -> <key>`` that is not seeded."""
    seeded = _seeded_keys()
    findings: list[str] = []
    for module_name, attr_name, _mount_kind, mode in specs:
        for key in _derived_permission_keys(mode):
            if key not in seeded:
                findings.append(f"{module_name}:{attr_name} mode={mode!r} -> {key!r}")
    return sorted(findings)


def _production_specs() -> list[RouterSpec]:
    return [*_CORE_ROUTER_SPECS, *_DEFERRED_API_ROUTER_SPECS]


def test_every_mounted_permission_key_is_seeded() -> None:
    unseeded = _unseeded_mount_keys(_production_specs())
    assert not unseeded, (
        "These router mounts derive permission keys that are absent from "
        "DEFAULT_PERMISSIONS. require_permission fails closed on an unknown "
        "key, so each of these routers 403s for every non-admin principal:\n  "
        + "\n  ".join(unseeded)
    )


def test_the_mount_key_guard_still_bites() -> None:
    """Two-sided sensitivity proof: plant the historical defect, then remove it.

    Without this, ``test_every_mounted_permission_key_is_seeded`` could pass
    because the derivation quietly stopped matching any mount at all.
    """
    planted: RouterSpec = (
        "app.api.planted",
        "router",
        "api",
        "perm:operations:dispatch:read",
    )

    # (a) planted bad mount is reported, with BOTH derived keys named
    reported = _unseeded_mount_keys([*_production_specs(), planted])
    assert reported == [
        "app.api.planted:router mode='perm:operations:dispatch:read' -> "
        "'operations:dispatch:read:read'",
        "app.api.planted:router mode='perm:operations:dispatch:read' -> "
        "'operations:dispatch:read:write'",
    ], reported

    # (b) removing it goes quiet
    assert not _unseeded_mount_keys(_production_specs())


def test_permission_mount_prefixes_are_covered() -> None:
    """A new key-deriving mount mode must extend ``_derived_permission_keys``.

    ``_router_dependencies`` dispatches on ``mode.startswith(...)``; if someone
    adds a third permission-deriving prefix there, this guard would silently
    stop covering it.
    """
    source = inspect.getsource(_router_dependencies)
    prefixes = set(re.findall(r'mode\.startswith\("([^"]+)"\)', source))
    assert prefixes == {_PERM_PREFIX, _READPERM_PREFIX}, (
        "app.main._router_dependencies handles permission mount prefixes "
        f"{sorted(prefixes)} but this guard only derives keys for "
        f"{sorted({_PERM_PREFIX, _READPERM_PREFIX})}. Extend "
        "_derived_permission_keys in the same change."
    )
