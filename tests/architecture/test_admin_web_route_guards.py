"""Every mutating admin-web route must declare a permission guard.

The admin web router mounts under ``require_admin_web_auth`` — a staff baseline
(default-deny to ``system_user``), NOT fine-grained authorization. Reduced-
privilege staff roles exist (auditor/operator/support/finance_manager), so a
mutating ``/admin`` route with no ``require_permission`` lets any logged-in staffer
perform it regardless of role.

The sibling ``test_route_permission_guards`` covers only ``/api/v1``; this is its
admin-web counterpart. It walks the assembled admin router and fails the build on
any POST/PUT/PATCH/DELETE route lacking a permission/role guard, unless its path is
genuinely self-scoped (own profile / own MFA).

Backfilled to ZERO unguarded routes on 2026-06-29 (the route-authz hardening +
catalog/gis/reseller/legal follow-up), so ``_KNOWN_UNGUARDED`` is empty — any new
hole is build-failing immediately. Do not add to it.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.web.admin import router as admin_router

# Dependency callables that constitute a real authorization guard (mirrors the
# /api/v1 guard test).
_GUARD_NAMES = {
    "_require_permission",
    "_require_role",
    "_require_any_permission",
    "_require_method_permission",
    "require_scoped_permission",
    "_require_scoped_permission",
}

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Self-scoped surfaces: a staffer manages their OWN profile / MFA, so no extra
# permission applies beyond the staff baseline.
_ALLOWLIST_PREFIXES = ("/admin/system/users/profile",)

# Burn-down backlog of pre-existing unguarded routes. Empty = fully closed; any
# new mutating route without a guard fails the build. Do NOT add to this.
_KNOWN_UNGUARDED: set[str] = set()


def _dependency_calls(dependant) -> set[str]:
    names: set[str] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names |= _dependency_calls(sub)
    return names


def _is_allowlisted(path: str) -> bool:
    return any(path.startswith(p) for p in _ALLOWLIST_PREFIXES)


def test_all_mutating_admin_web_routes_declare_a_permission_guard():
    unguarded: list[str] = []
    seen_quarantined: set[str] = set()
    for route in admin_router.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = set(route.methods or []) & _MUTATING_METHODS
        if not methods:
            continue
        if _dependency_calls(route.dependant) & _GUARD_NAMES:
            continue
        if _is_allowlisted(route.path):
            continue
        entry = f"{','.join(sorted(methods))} {route.path}"
        if entry in _KNOWN_UNGUARDED:
            seen_quarantined.add(entry)
            continue
        unguarded.append(entry)

    assert not unguarded, (
        "These mutating /admin routes have no permission guard (any logged-in "
        "staffer can call them regardless of role). Add a require_permission "
        "dependency, or allowlist the path if it is genuinely self-scoped:\n  "
        + "\n  ".join(sorted(unguarded))
    )

    stale = _KNOWN_UNGUARDED - seen_quarantined
    assert not stale, (
        "These routes are in the unguarded quarantine but are now guarded or "
        "gone — remove them from _KNOWN_UNGUARDED:\n  " + "\n  ".join(sorted(stale))
    )
