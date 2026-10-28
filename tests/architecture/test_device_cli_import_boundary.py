"""``dotmac-device-cli`` has exactly one importer: the host adapter module.

The package is stateless dialogue and profile knowledge (starter ADR-0077). Sub
adopts it behind default-off flags, and every Sub-specific concern (credentials,
transport, slot, logout, metrics, settings, result mapping) lives in
``app/services/network/device_cli_host.py``. A second importer would let package
types leak into callers whose signatures are deliberately unchanged, and would
let a caller reach the dialogue engine without the fail-closed host guards.

Three guards, no baseline:

* only the host adapter module may import the package (anywhere under ``app/``,
  ``scripts/`` or ``alembic/``);
* the host only names the package's published surface (``SUPPORTED_MODULES``);
* only the two wired readers may import the host (a new caller is a new read
  cutover and must update this list in the same change).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("app", "scripts", "alembic")

PACKAGE = "dotmac_device_cli"
HOST_MODULE = "app/services/network/device_cli_host.py"
HOST_DOTTED = "app.services.network.device_cli_host"

#: Modules that may import the host adapter (lazily, inside the public function
#: they wire). S1 wires exactly ``version`` and ``list_onts``.
ALLOWED_HOST_IMPORTERS = frozenset(
    {
        "app/services/network/olt_ssh.py",  # get_firmware_info -> version
        "app/services/network/olt_ssh_ont/status.py",  # get_registered_ont_serials
    }
)


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            found.append((node.lineno, module))
            # `from app.services.network import device_cli_host`
            found.extend(
                (node.lineno, f"{module}.{alias.name}") for alias in node.names
            )
    return found


def _files() -> list[Path]:
    return sorted(
        path
        for directory in SCAN_DIRS
        if (ROOT / directory).is_dir()
        for path in (ROOT / directory).rglob("*.py")
    )


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _names_package(module: str) -> bool:
    return module == PACKAGE or module.startswith(f"{PACKAGE}.")


def test_only_the_host_adapter_imports_the_device_cli_package() -> None:
    offenders = [
        f"{_rel(path)}:{line}:{module}"
        for path in _files()
        for line, module in _imports(path)
        if _names_package(module) and _rel(path) != HOST_MODULE
    ]
    assert not offenders, (
        "dotmac-device-cli may only be imported by "
        f"{HOST_MODULE}; map its results there.\n  " + "\n  ".join(offenders)
    )


def test_the_host_names_only_the_published_package_surface() -> None:
    import dotmac_device_cli

    used = {
        module for _, module in _imports(ROOT / HOST_MODULE) if _names_package(module)
    }
    assert used, "the scan found no package import in the host adapter"
    published = {PACKAGE} | set(dotmac_device_cli.SUPPORTED_MODULES)
    # `from pkg.module import name` also yields `pkg.module.name`: the name is an
    # attribute of a published module, not a module of its own.
    unpublished = sorted(
        module
        for module in used
        if module not in published and module.rsplit(".", 1)[0] not in published
    )
    assert not unpublished, f"host imports unpublished modules: {unpublished}"


def test_only_the_wired_readers_import_the_host() -> None:
    offenders = [
        f"{_rel(path)}:{line}"
        for path in _files()
        for line, module in _imports(path)
        if (module == HOST_DOTTED or module.startswith(f"{HOST_DOTTED}."))
        if _rel(path) not in ALLOWED_HOST_IMPORTERS and _rel(path) != HOST_MODULE
    ]
    assert not offenders, (
        "a new caller of the device-cli host adapter is a new read cutover: add "
        "it to ALLOWED_HOST_IMPORTERS here, behind the default-off flags.\n  "
        + "\n  ".join(offenders)
    )


def test_the_wired_readers_really_import_the_host() -> None:
    """Keeps the allow-list honest: a stale entry would hide a future importer."""
    actual = {
        _rel(path)
        for path in _files()
        for _, module in _imports(path)
        if module == HOST_DOTTED
    }
    assert actual == ALLOWED_HOST_IMPORTERS


def test_the_scan_would_actually_find_an_import() -> None:
    host = ROOT / HOST_MODULE
    assert any(_names_package(m) for _, m in _imports(host))
    assert HOST_MODULE in {_rel(p) for p in _files()}
