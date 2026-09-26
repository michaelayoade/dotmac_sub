"""Code-owned module registry for centrally governed custom fields."""

from __future__ import annotations

from collections import Counter

from app.services.custom_field_contracts import (
    CustomFieldModuleManifest,
    CustomFieldTargetCapability,
)
from app.services.sot_registry.registry import DOMAIN_SOT_RELATIONSHIPS


class CustomFieldCapabilityError(ValueError):
    """Raised when a caller names an undeclared or ambiguous target."""


def _label(domain: str) -> str:
    return domain.replace("_", " ").title()


def all_module_manifests() -> tuple[CustomFieldModuleManifest, ...]:
    """Return all SOT modules, including modules that have not opted in."""

    return tuple(
        CustomFieldModuleManifest(
            module_key=domain.domain,
            label=_label(domain.domain),
            owner_domain=domain.domain,
            registered=domain.custom_fields is not None,
            targets=domain.custom_fields.targets if domain.custom_fields else (),
            legacy_surfaces=(
                domain.custom_fields.legacy_surfaces if domain.custom_fields else ()
            ),
            manifest_version=(
                domain.custom_fields.manifest_version if domain.custom_fields else None
            ),
        )
        for domain in DOMAIN_SOT_RELATIONSHIPS
    )


def registered_module_manifests() -> tuple[CustomFieldModuleManifest, ...]:
    return tuple(item for item in all_module_manifests() if item.registered)


def target_capability(key: str) -> CustomFieldTargetCapability:
    normalized = key.strip().casefold()
    matches = [
        target
        for module in registered_module_manifests()
        for target in module.targets
        if target.key.casefold() == normalized
    ]
    if len(matches) != 1:
        raise CustomFieldCapabilityError(
            f"Custom-field target {key!r} is not declared exactly once."
        )
    return matches[0]


def _duplicates(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(key for key, count in Counter(values).items() if count > 1))


def capability_registry_errors() -> tuple[str, ...]:
    """Return every structural declaration error; CI fails on any result."""

    errors: list[str] = []
    manifests = all_module_manifests()
    registered = registered_module_manifests()
    targets = tuple(target for item in registered for target in item.targets)
    legacy = tuple(surface for item in registered for surface in item.legacy_surfaces)
    errors.extend(
        f"duplicate custom-field module key {key!r}"
        for key in _duplicates(tuple(item.module_key for item in manifests))
    )
    errors.extend(
        f"duplicate custom-field target key {key!r}"
        for key in _duplicates(tuple(item.key for item in targets))
    )
    errors.extend(
        f"duplicate legacy custom-field surface key {key!r}"
        for key in _duplicates(tuple(item.key for item in legacy))
    )
    for manifest in registered:
        if manifest.manifest_version != 1:
            errors.append(
                f"custom-field module {manifest.module_key!r} uses unsupported "
                f"manifest version {manifest.manifest_version!r}"
            )
        if not manifest.targets and not manifest.legacy_surfaces:
            errors.append(
                f"custom-field module {manifest.module_key!r} declares no surfaces"
            )
        for target in manifest.targets:
            if not all(
                value.strip()
                for value in (
                    target.key,
                    target.label,
                    target.entity_id_type,
                    target.read_permission,
                    target.write_permission,
                    target.detail_path_template,
                )
            ):
                errors.append(f"custom-field target {target.key!r} is incomplete")
            if target.entity_id_type != "uuid":
                errors.append(
                    f"custom-field target {target.key!r} uses unsupported identity type"
                )
            if "{target_id}" not in target.detail_path_template:
                errors.append(
                    f"custom-field target {target.key!r} has no target path placeholder"
                )
            if target.maximum_active_fields < 1:
                errors.append(
                    f"custom-field target {target.key!r} has an invalid field limit"
                )
    return tuple(sorted(errors))


def require_valid_capability_registry() -> None:
    errors = capability_registry_errors()
    if errors:
        raise CustomFieldCapabilityError("; ".join(errors))


__all__ = [
    "CustomFieldCapabilityError",
    "all_module_manifests",
    "capability_registry_errors",
    "registered_module_manifests",
    "require_valid_capability_registry",
    "target_capability",
]
