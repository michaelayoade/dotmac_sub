"""Only documented public dotmac-kernel surfaces may ever be imported in app/.

Slice S1 of the selective kernel-adoption plan
(docs/PLATFORM_ADOPTION_LEDGER.md, "Kernel import allowlist") lands this guard
BEFORE the dependency exists: today there are zero ``dotmac_kernel`` imports
anywhere under ``app/``, and the first one that is not on the documented
allowlist must be a build failure, not a review finding. Detection is
AST-based over source files — it never imports ``dotmac_kernel`` itself, so
the guard works while the package is not installed.

What the allowlist encodes (the ledger is the narrative authority):

- Only the consume-pure/adapt modules from the ledger may appear in ``app/``
  (``assembly``, ``capabilities``, ``features``, ``money``, ``profiles``,
  ``providers``, ``providers.provisioning``).
- Bare ``import dotmac_kernel`` / ``from dotmac_kernel import X`` is forbidden:
  the top-level package re-exports identity, audit, and entitlement names
  indiscriminately, and Sub imports must name the specific submodule.
- ``mount_features`` is app-factory machinery and may not be imported even
  from the allowlisted ``dotmac_kernel.features``.
- Everything else — ``db``, ``models``, ``app_factory``, ``deps``,
  ``middleware.*``, ``messaging.*``, any ``dotmac_kernel._*`` — is rejected,
  whether "supported" by the kernel or not: defer-db and prohibited classes
  stay out of ``app/`` until a ledger amendment moves them.

KNOWN LIMIT (disclosed, not hidden): a name reached through an allowed module
object (``from dotmac_kernel import features`` is already rejected, but
``from dotmac_kernel.features import mount_features as mf`` is caught while
``features.mount_features`` attribute access after ``import
dotmac_kernel.features`` is not). The module-level rejection of every
non-allowlisted module keeps that gap to attributes of the seven allowed
metadata modules only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"
LEDGER = PROJECT_ROOT / "docs" / "PLATFORM_ADOPTION_LEDGER.md"

#: The consume-pure/adapt kernel modules from the ledger — the only
#: ``dotmac_kernel`` modules ``app/`` may ever import. Grows only with a
#: ledger amendment in the same change.
ALLOWED_KERNEL_MODULES = frozenset(
    {
        "dotmac_kernel.assembly",
        "dotmac_kernel.capabilities",
        "dotmac_kernel.features",
        "dotmac_kernel.machine_auth",
        "dotmac_kernel.models",
        "dotmac_kernel.money",
        "dotmac_kernel.prerequisites",
        "dotmac_kernel.profiles",
        "dotmac_kernel.providers",
        "dotmac_kernel.providers.provisioning",
        "dotmac_kernel.secret_sources",
        "dotmac_kernel.setting_scopes",
        "dotmac_kernel.setting_value_types",
        "dotmac_kernel.settings_cache",
        "dotmac_kernel.settings_crypto",
        "dotmac_kernel.settings_models",
        "dotmac_kernel.settings_resolver",
    }
)

#: Names that stay forbidden even when their defining module is allowlisted.
DENIED_NAMES = frozenset(
    {
        ("dotmac_kernel.features", "mount_features"),
    }
)

#: Modules allowlisted for a NAMED subset only — every other name in them stays
#: forbidden, and a bare ``import`` of the module is refused because it would
#: reach all of them.
#:
#: ``dotmac_kernel.models`` is the Party/identity family. ADR-0009 admits the
#: operator-tenant bridge and nothing else: Sub identity is not replaced, and
#: `Party`, `PartyRole`, `Role` and `UserCredential` each collide with a Sub
#: model of the same name, so a careless import would shadow one.
#:
#: The settings resolver legitimately uses the kernel's persistence classes
#: internally against Sub's adopted table shape. App code needs only the open
#: ``SettingDomain`` value type from ``settings_models``; importing
#: ``DomainSetting`` or ``DomainSettingHistory`` directly would create a second
#: model/writer surface over two same-named Sub tables.
#: ``dotmac_kernel.prerequisites`` is admitted for the composition VOCABULARY —
#: the effect names a binding is keyed by, the binding type itself, and the
#: installer ``alembic/env.py`` calls. ``require_prerequisites``, the function
#: that PROVES an effect against the live catalog, is deliberately absent: it
#: stays where it is, called by the module's own migration inside its own
#: lineage. Sub reads the vocabulary; it does not run the check. The ledger
#: claimed that narrowing in prose while nothing enforced it, so the name was
#: importable here — this entry is what makes the sentence true.
RESTRICTED_MODULE_NAMES: dict[str, frozenset[str]] = {
    "dotmac_kernel.models": frozenset({"Tenant", "TenantDomain"}),
    "dotmac_kernel.settings_models": frozenset({"SettingDomain"}),
    "dotmac_kernel.prerequisites": frozenset(
        {
            "MODULE_DATABASE_ROLES_V1",
            "PrerequisiteBinding",
            "TENANT_SCOPE_CATALOG_V1",
            "install_prerequisite_bindings",
        }
    ),
}


def _kernel_import_violations(root: Path) -> list[str]:
    """Return every ``dotmac_kernel`` import under ``root`` that breaks the ledger.

    Pure static scan: parses source with ``ast``, never imports the package.
    """
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name != "dotmac_kernel" and not name.startswith(
                        "dotmac_kernel."
                    ):
                        continue
                    if name not in ALLOWED_KERNEL_MODULES:
                        violations.append(
                            f"{rel}:{node.lineno}: import {name} — not on the "
                            "ledger allowlist"
                        )
                    elif name in RESTRICTED_MODULE_NAMES:
                        permitted = ", ".join(sorted(RESTRICTED_MODULE_NAMES[name]))
                        violations.append(
                            f"{rel}:{node.lineno}: import {name} — reaches every "
                            f"name in it; import only {permitted} by name"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level or (
                    module != "dotmac_kernel"
                    and not module.startswith("dotmac_kernel.")
                ):
                    continue
                if module == "dotmac_kernel":
                    violations.append(
                        f"{rel}:{node.lineno}: from dotmac_kernel import ... — "
                        "name the specific allowlisted submodule"
                    )
                    continue
                for alias in node.names:
                    # `from dotmac_kernel.providers import provisioning` imports
                    # the submodule; judge it as that submodule.
                    dotted = f"{module}.{alias.name}"
                    if dotted in ALLOWED_KERNEL_MODULES:
                        continue
                    if module not in ALLOWED_KERNEL_MODULES:
                        violations.append(
                            f"{rel}:{node.lineno}: from {module} import "
                            f"{alias.name} — module not on the ledger allowlist"
                        )
                    elif (module, alias.name) in DENIED_NAMES:
                        violations.append(
                            f"{rel}:{node.lineno}: from {module} import "
                            f"{alias.name} — name is denied by the ledger"
                        )
                    elif (
                        module in RESTRICTED_MODULE_NAMES
                        and alias.name not in RESTRICTED_MODULE_NAMES[module]
                    ):
                        permitted = ", ".join(sorted(RESTRICTED_MODULE_NAMES[module]))
                        violations.append(
                            f"{rel}:{node.lineno}: from {module} import "
                            f"{alias.name} — only {permitted} are admitted "
                            "(ADR-0009)"
                        )
    return violations


def test_app_imports_only_allowlisted_kernel_modules() -> None:
    """Today this passes with zero kernel imports; it is the S2+ boundary."""
    scanned = list(APP_DIR.rglob("*.py"))
    assert len(scanned) > 100, "app/ scan found suspiciously few files"
    violations = _kernel_import_violations(APP_DIR)
    assert not violations, (
        "Forbidden dotmac_kernel import(s) in app/ — the allowlist is "
        "docs/PLATFORM_ADOPTION_LEDGER.md 'Kernel import allowlist', amended "
        "only with a ledger change:\n" + "\n".join(violations)
    )


def test_alembic_imports_only_allowlisted_kernel_modules() -> None:
    """`alembic/` is in scope too, because composition put a kernel call there.

    Composing a module lineage means `env.py` calls
    `install_prerequisite_bindings`. That is a real kernel import outside `app/`,
    and until this test existed the whole directory was unmonitored rather than
    exempt — the migration entry point could have imported anything at all and
    the `app/`-scoped scan would have stayed green.
    """

    scanned = list(ALEMBIC_DIR.rglob("*.py"))
    # Sensitivity: assert the scan actually reaches the one file that matters,
    # so a future layout change cannot quietly empty this test's input.
    assert ALEMBIC_DIR / "env.py" in scanned, "alembic/env.py is not being scanned"
    violations = _kernel_import_violations(ALEMBIC_DIR)
    assert not violations, (
        "Forbidden dotmac_kernel import(s) in alembic/ — the allowlist is "
        "docs/PLATFORM_ADOPTION_LEDGER.md 'Kernel import allowlist', amended "
        "only with a ledger change:\n" + "\n".join(violations)
    )


def test_guard_fails_on_forbidden_kernel_imports(tmp_path: Path) -> None:
    """Negative control: the checker must actually flag internal imports.

    A synthetic tree exercises every rejection class plus one allowed import,
    proving the guard is red-sensitive rather than vacuously green while the
    dependency does not exist.
    """
    (tmp_path / "offender.py").write_text(
        "import dotmac_kernel.db\n"
        "from dotmac_kernel import Party\n"
        "from dotmac_kernel.models import UserCredential\n"
        "import dotmac_kernel.models\n"
        "from dotmac_kernel.features import mount_features\n"
        "from dotmac_kernel._internal import anything\n"
        "from dotmac_kernel.money import Money\n"
        "from dotmac_kernel.providers import provisioning\n"
        "from dotmac_kernel.models import Tenant, TenantDomain\n"
        "from dotmac_kernel.settings_models import DomainSetting\n"
        "from dotmac_kernel.settings_models import SettingDomain\n"
        # Appended AFTER the eleven above on purpose: the admitted-import
        # assertions below pin lines 9 and 11, so new cases must not shift them.
        "from dotmac_kernel.prerequisites import require_prerequisites\n"
        "from dotmac_kernel.prerequisites import PrerequisiteBinding\n",
        encoding="utf-8",
    )
    violations = _kernel_import_violations(tmp_path)
    assert len(violations) == 8, violations
    flagged = "\n".join(violations)
    for needle in (
        "dotmac_kernel.db",
        "from dotmac_kernel import",
        "dotmac_kernel.models",
        "mount_features",
        "dotmac_kernel._internal",
        # ADR-0009 admits two names from `models`; `UserCredential` is not one,
        # and a bare `import dotmac_kernel.models` reaches all of them.
        "UserCredential",
        "reaches every name",
        "DomainSetting",
        # Sub reads the composition vocabulary; it does not run the check. This
        # is the name the ledger forbids in prose, now forbidden in fact.
        "require_prerequisites",
    ):
        assert needle in flagged, f"checker missed {needle!r}: {violations}"
    assert "dotmac_kernel.money" not in flagged, violations
    # The admitted import must NOT be flagged — otherwise the narrowing is
    # indistinguishable from the module still being off the allowlist. Asserted
    # by LINE rather than by substring: the rejection message for a bare
    # `import dotmac_kernel.models` legitimately names Tenant and TenantDomain
    # as the permitted imports, so a substring check matches the guidance and
    # not the subject.
    admitted_line = "offender.py:9:"
    assert not [v for v in violations if v.startswith(admitted_line)], violations
    admitted_settings_line = "offender.py:11:"
    assert not [v for v in violations if v.startswith(admitted_settings_line)], (
        violations
    )
    # The binding TYPE is admitted while `require_prerequisites` on the line
    # above it is not, which is the whole point of the narrowing: same module,
    # different names, opposite verdicts.
    admitted_binding_line = "offender.py:13:"
    assert not [v for v in violations if v.startswith(admitted_binding_line)], (
        violations
    )


def test_allowlist_matches_the_ledger() -> None:
    """The ledger section and this test's frozenset must stay in exact sync.

    The ledger is the reviewed narrative authority; this file is the enforcer.
    Either drifting from the other is the failure mode this pin exists for.
    """
    text = LEDGER.read_text(encoding="utf-8")
    match = re.search(
        r"## Kernel import allowlist.*?\n(.*?)\n## ", text, flags=re.DOTALL
    )
    assert match, "ledger is missing the 'Kernel import allowlist' section"
    documented = set(re.findall(r"^- `(dotmac_kernel[\w.]*)`", match.group(1), re.M))
    assert documented == set(ALLOWED_KERNEL_MODULES), (
        "docs/PLATFORM_ADOPTION_LEDGER.md allowlist and "
        "ALLOWED_KERNEL_MODULES disagree.\n"
        f"only in ledger: {sorted(documented - ALLOWED_KERNEL_MODULES)}\n"
        f"only in test:   {sorted(ALLOWED_KERNEL_MODULES - documented)}"
    )
