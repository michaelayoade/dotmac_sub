"""Pure permission vocabulary owned by auth.permission_gate."""


def _permission_domain_aliases(permission_key: str) -> list[str]:
    """Return explicit compatibility aliases for permission-key transitions.

    Legacy report API-key scopes remain read-only aliases during the granular
    reports migration. They intentionally do not satisfy ``:export``.
    """
    aliases = [permission_key]
    if permission_key.startswith("customer:"):
        aliases.append(permission_key.replace("customer:", "subscriber:", 1))
    elif permission_key.startswith("subscriber:"):
        aliases.append(permission_key.replace("subscriber:", "customer:", 1))
    legacy_report_read_scope = {
        "reports:billing:read": "reports:billing",
        "reports:network:read": "reports:network",
    }.get(permission_key)
    if legacy_report_read_scope:
        aliases.append(legacy_report_read_scope)
    return aliases


def _expand_permission_key_single(permission_key: str) -> list[str]:
    """
    Expand a permission key to aliases for the same permission.

    Permission checks are intentionally exact. A route requiring
    ``billing:invoice:create`` is not satisfied by ``billing:write`` unless the
    principal has an explicit wildcard grant handled by ``_wildcard_ancestors``
    or is in the admin role. This keeps broad/admin-only permissions from
    overriding granular role containers.
    """
    return [permission_key]


def _wildcard_ancestors(permission_key: str) -> list[str]:
    """Wildcard keys that, if HELD, satisfy ``permission_key``.

    ``network:nas:write`` -> ``["*", "network:*", "network:nas:*"]``. A role
    or direct grant of any of these authorizes the requirement, so access can
    be granted by domain (``network:*``) or globally (``*``) instead of
    enumerating every leaf permission.
    """
    parts = permission_key.split(":")
    ancestors = ["*"]
    for i in range(1, len(parts)):
        ancestors.append(":".join(parts[:i]) + ":*")
    return ancestors


def _expand_permission_keys(permission_key: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for alias in _permission_domain_aliases(permission_key):
        for key in _expand_permission_key_single(alias):
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
    # Wildcard grants satisfy any matching requirement.
    for key in _wildcard_ancestors(permission_key):
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def permission_keys_allow(permission_keys: frozenset[str], required: str) -> bool:
    """Match effective grants using the canonical aliases and wildcards."""
    return bool(permission_keys.intersection(_expand_permission_keys(required)))
