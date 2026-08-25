"""Pure contract for the composed-module outbox dispatcher identities.

Role creation is an explicitly elevated cluster operation.  Migrations and
online application paths may verify these identities, but only
``scripts/bootstrap_outbox_dispatcher_roles.py`` creates or repairs them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

RolePosture = tuple[bool, bool, bool]

#: Exact ``(rolcanlogin, rolbypassrls, rolsuper)`` contract.  A dispatcher is a
#: login only so it can call its plane's two SECURITY DEFINER functions.  It
#: must not bypass RLS and must never be a superuser.
RELAY_DISPATCHER_CONTRACT: Final[dict[str, RolePosture]] = {
    "outbox_dispatcher": (True, False, False),
    "platform_outbox_dispatcher": (True, False, False),
}


def relay_dispatcher_violations(
    observed: Mapping[str, RolePosture],
) -> tuple[str, ...]:
    """Describe every absent or wrong-shaped dispatcher without mutating it."""

    violations: list[str] = []
    for role, expected in RELAY_DISPATCHER_CONTRACT.items():
        actual = observed.get(role)
        if actual is None:
            violations.append(f"database role {role!r} is missing")
        elif actual != expected:
            violations.append(
                f"{role} has (rolcanlogin, rolbypassrls, rolsuper)={actual!r}; "
                f"expected {expected!r}"
            )
    return tuple(violations)


__all__ = ["RELAY_DISPATCHER_CONTRACT", "RolePosture", "relay_dispatcher_violations"]
