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
* only the wired reader may import the host (a new caller is a new read
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
#: they wire). S1 wires ``list_onts`` only.
ALLOWED_HOST_IMPORTERS = frozenset(
    {"app/services/network/olt_ssh_ont/status.py"}  # get_registered_ont_serials
)
GATE_MODULE = "app/services/network/device_cli_gate.py"


def _package_of(path: Path) -> list[str]:
    """Dotted package parts of ``path`` (``__init__`` is its own directory)."""
    return list(path.relative_to(ROOT).with_suffix("").parts)[:-1]


def _resolve_relative(package: list[str], level: int, module: str | None) -> str:
    base = package[: len(package) - (level - 1)]
    return ".".join([*base, *([module] if module else [])])


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                module = _resolve_relative(_package_of(path), node.level, node.module)
            else:
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


def test_the_gate_imports_neither_the_package_nor_the_host() -> None:
    """The kill switch must work when the package cannot even import."""
    named = [
        module
        for _, module in _imports(ROOT / GATE_MODULE)
        if _names_package(module)
        or module == HOST_DOTTED
        or module.startswith(f"{HOST_DOTTED}.")
    ]
    assert not named, f"the gate must not import: {named}"


def test_relative_imports_are_resolved_by_the_scan() -> None:
    package = ["app", "services", "network"]
    assert _resolve_relative(package, 1, "device_cli_host") == HOST_DOTTED
    assert _resolve_relative(package, 1, None) == "app.services.network"
    assert _resolve_relative(package, 2, "x") == "app.services.x"
    assert _package_of(ROOT / "app/services/network/olt_ssh.py") == package
    assert _package_of(ROOT / "app/services/network/__init__.py") == package


def test_nothing_in_app_calls_acquire_device_slot_yet() -> None:
    """The package slot wrapper is proven but not on any production path in S1.

    Wiring it is the limiter-swap PR, which must also decide the
    limiter-disabled lane (the legacy null lease has no package equivalent).
    """
    offenders = []
    for path in _files():
        if _rel(path) == HOST_MODULE:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else None
            )
            if name == "acquire_device_slot":
                offenders.append(f"{_rel(path)}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "acquire_device_slot" for alias in node.names
            ):
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert not offenders, "acquire_device_slot is not wired yet:\n  " + "\n  ".join(
        offenders
    )


# ── _open_shell( ratchet ─────────────────────────────────────────────────────

OPEN_SHELL_BASELINE = Path(__file__).with_name(
    "open_shell_outside_olt_ssh_baseline.txt"
)
OPEN_SHELL_EXEMPT = {"app/services/network/olt_ssh.py", HOST_MODULE}


def _open_shell_calls() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _files():
        rel = _rel(path)
        if rel in OPEN_SHELL_EXEMPT or not rel.startswith("app/"):
            continue
        n = sum(
            1
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "_open_shell")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_open_shell"
                )
            )
        )
        if n:
            counts[rel] = n
    return counts


def _baseline() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in OPEN_SHELL_BASELINE.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, count = line.rsplit(" ", 1)
            out[name] = int(count)
    return out


def test_open_shell_call_sites_outside_olt_ssh_only_shrink() -> None:
    """Every legacy dialogue opener is a future swap target: count them per file,
    never grow. The host adapter reuses ``_open_shell`` from ONE helper and is
    exempt with ``olt_ssh.py`` itself."""
    baseline = _baseline()
    grown = {
        file: (count, baseline.get(file, 0))
        for file, count in _open_shell_calls().items()
        if count > baseline.get(file, 0)
    }
    assert not grown, f"_open_shell( call sites grew (now, baseline): {grown}"


def test_the_open_shell_baseline_has_no_stale_headroom() -> None:
    """Lower the baseline when a call site is removed, so it cannot regrow."""
    actual = _open_shell_calls()
    stale = {
        file: (count, actual.get(file, 0))
        for file, count in _baseline().items()
        if count > actual.get(file, 0)
    }
    assert not stale, f"lower the baseline (baseline, actual): {stale}"


def test_the_host_opens_shells_from_exactly_one_site() -> None:
    sites = [
        node
        for node in ast.walk(
            ast.parse((ROOT / HOST_MODULE).read_text(encoding="utf-8"))
        )
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_open_shell"
    ]
    assert len(sites) == 1
