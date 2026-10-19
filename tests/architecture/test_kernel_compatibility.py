"""The pinned dotmac-kernel wheel is compatible with Sub, and stays contained.

Slice S2 of the selective kernel-adoption plan
(docs/PLATFORM_ADOPTION_LEDGER.md) pinned the wheel while Sub's runtime stayed
UNCHANGED. That is no longer the state and this file says so rather than
asserting a claim it has outgrown: the settings cutover made
``app/services/settings_spec.py`` register Sub's specs with
``dotmac_kernel.settings_resolver`` and resolve through it, so Sub now CONSUMES
the kernel rather than merely depending on it.

What this file proves, with zero skips:

- every ledger-allowlisted pure surface (and the tests/-only test kit) imports
  in a subprocess with ``DATABASE_URL`` and all DB-ish env stripped — the
  "testing is DB-free" claim is exercised, not assumed;
- the installed distribution is exactly the reviewed pin and ships ``py.typed``;
- ``pyproject.toml`` pins the kernel EXACTLY (no ``^``/``~``/``>=``/``*``
  range) from the named private index — an unreviewed range upgrade is a CI
  failure, not a lockfile surprise;
- the pure value contracts behave (exact ``Decimal`` money, float rejection,
  provisioning protocol/result types, assembly/feature/capability/profile
  specs, the reusable provider contract check);
- the Sub app builds with the kernel installed, and every ``dotmac_kernel``
  module in its import graph is one the ledger allowlist admits OR one the
  kernel reaches for itself (``TRANSITIVE_KERNEL_MODULES``, a reviewed
  snapshot — fifty-eight of them, which is worth knowing) — and no kernel
  middleware is mounted, no kernel route endpoint is served, and the top-level
  route prefix set is exactly the reviewed pin.

The containment that matters is unchanged: ``dotmac_kernel.deps``,
``.middleware``, ``.audit`` and the rest stay out of ``app/`` entirely, and a
side-door import of one fails here even though the AST guard cannot see it.

``dotmac_kernel.testing`` usage stays confined to ``tests/`` per the ledger;
this file is itself test-side and imports kernel modules freely (the S1 guard
covers ``app/`` only).
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryFile
from uuid import uuid4

import pytest

# The one allowlist, imported rather than restated: two copies would drift,
# and the ledger already keeps THAT module's copy in byte-for-byte sync.
from tests.architecture.test_kernel_import_boundary import ALLOWED_KERNEL_MODULES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

#: The reviewed kernel pin. Changing it is a ledger amendment
#: (docs/PLATFORM_ADOPTION_LEDGER.md), never a lockfile side effect.
KERNEL_PIN = "0.1.0a105"
KERNEL_WHEEL_SHA256 = "f8937e5525c3efd27e2030bf69ed0c37e137c345e376668a79e00ebf349b6ffa"
KERNEL_SDIST_SHA256 = "76a3ccb7d0344befe565681b710edfd3a563e64127b801e43bb239d5bc5b80d2"

#: The private index source name pyproject must route the kernel through.
KERNEL_SOURCE = "forgejo"

#: What the kernel loads FOR ITS OWN USE once `app/` imports the settings
#: resolver, measured rather than assumed — and larger than anyone expected.
#:
#: Consuming Kernel and Template Studio surfaces pulls thirty-one more modules
#: into the process,
#: including `audit`, `security`, `identity`, `permissions` and `entitlements`
#: — precisely the surfaces the adoption ledger keeps out of `app/`. Nothing in
#: `app/` imports them and the AST guard still refuses one that tries; they are
#: here because `settings_resolver` reaches them internally.
#:
#: The a50 -> a81 repin added five names, each traced to an ALLOWED import
#: rather than absorbed: `planes` and `prerequisites` arrive through
#: `assembly`/`modules` (the ADR-0028 explicit plane contract), `external_identity`
#: through `models` (kernel revision 0024's binding table), `outbox_event_types`
#: through `features`, and the private `_transactions` through the a73 change
#: that stopped consent/delivery/idempotency/external-identity importing the
#: eager kernel database owner just to open a SAVEPOINT. None of them is an
#: authority Sub consults, and none is reachable from `app/`.
#: The a81 -> a90 repin adds `machine_auth` and `machine_models`; they are
#: loaded by the kernel's machine-credential facility but are not direct Sub
#: imports and mount no runtime surface in `app.main`.
#: The a94 -> a97 repin adds `machine_rotation` and `source_applications` through
#: the Kernel package root, `web_surfaces` through Sub's allowlisted assembly
#: metadata import, and `route_metadata` through the allowlisted capability
#: and permission declarations. These imports do not mount Kernel routes or
#: transfer application authority; the route and middleware tests below still
#: enforce that boundary.
#: Template Studio a5's supported service import also loads `flag_models`
#: because published rendering can consult its declared strict-render flag.
#: Sub will pass strictness explicitly at its content cutover; loading this
#: helper does not install Studio's Kernel-owned route or flag policy in Sub.
#:
#: a98 adds capability_composition and capability_contract via the package root.
#: Their imports only reach the already-reviewed product_manifest contract.
#: a105's public API documentation, auth, catalog and session-runtime imports
#: are captured below as an exact transitive snapshot. Loading
#: session_runtime does not construct or bind a runtime; its reference factory
#: and DatabaseRuntime construction remain excluded from Sub boot.
#:
#: The 2026-10-17 money-pack pins (billing 0.1.0a8, payments 0.1.0a3) widen the
#: snapshot by thirteen: their model/service imports reach the kernel's
#: fingerprints, idempotency ledger and messaging (inbox/outbox/relay/worker)
#: modules at app import time. Loading them constructs no relay, worker or
#: ledger writer in Sub; the boot-construction sibling test still holds.
#:
#: Slice 6a (Starter issuance) adds `transactions`: `dotmac_billing.service`
#: imports `conflict_savepoint_requires_rollback` from it, and the issuance
#: lane now imports the billing service at app import time. The helper only
#: classifies savepoint conflicts inside the billing writer's own calls; Sub
#: constructs no transaction owner from it.
#:
#: Being LOADED is not being USED: a module in `sys.modules` creates no second
#: authority, mounts no route, and answers no question Sub asks. The sibling
#: test below proves that separately, on middleware and routes. But the cost of
#: adoption is not "one module" and this list is what stops that being a
#: comfortable assumption.
#:
#: This is a reviewed snapshot, like `EXPECTED_ROUTE_PREFIXES`. A NEW name
#: appearing means the kernel started reaching somewhere new, which is a thing
#: to look at rather than absorb. It is NOT a licence for `app/` to import any
#: of these — that list is `ALLOWED_KERNEL_MODULES`, and it is the boundary.
TRANSITIVE_KERNEL_MODULES = frozenset(
    {
        "dotmac_kernel._transactions",
        "dotmac_kernel.audit",
        "dotmac_kernel.audit_actions",
        "dotmac_kernel.api_documentation",
        "dotmac_kernel.branding",
        "dotmac_kernel.cache",
        "dotmac_kernel.capability_composition",
        "dotmac_kernel.capability_contract",
        "dotmac_kernel.config",
        "dotmac_kernel.database_catalog_comparator",
        "dotmac_kernel.deps",
        "dotmac_kernel.display",
        "dotmac_kernel.entitlements",
        "dotmac_kernel.exceptions",
        "dotmac_kernel.external_identity",
        "dotmac_kernel.facet_principal",
        "dotmac_kernel.fingerprints",
        "dotmac_kernel.flag_models",
        "dotmac_kernel.flags",
        "dotmac_kernel.idempotency",
        "dotmac_kernel.idempotency_models",
        "dotmac_kernel.identity",
        "dotmac_kernel.machine_auth",
        "dotmac_kernel.machine_models",
        "dotmac_kernel.machine_rotation",
        "dotmac_kernel.messaging",
        "dotmac_kernel.messaging.envelope",
        "dotmac_kernel.messaging.inbox",
        "dotmac_kernel.messaging.models",
        "dotmac_kernel.messaging.outbox",
        "dotmac_kernel.messaging.platform",
        "dotmac_kernel.messaging.platform_relay",
        "dotmac_kernel.messaging.platform_worker",
        "dotmac_kernel.messaging.relay",
        "dotmac_kernel.messaging.worker",
        "dotmac_kernel.models_platform",
        "dotmac_kernel.modules",
        "dotmac_kernel.namespaces",
        "dotmac_kernel.outbox_event_types",
        "dotmac_kernel.permissions",
        "dotmac_kernel.permission_provisioning",
        "dotmac_kernel.platform_auth",
        "dotmac_kernel.planes",
        "dotmac_kernel.prerequisites",
        "dotmac_kernel.product_database_catalog",
        "dotmac_kernel.product_manifest",
        "dotmac_kernel.query",
        "dotmac_kernel.route_metadata",
        "dotmac_kernel.semantic_encoding",
        "dotmac_kernel.security",
        "dotmac_kernel.session_runtime",
        "dotmac_kernel.setting_domains",
        "dotmac_kernel.setting_scopes",
        "dotmac_kernel.settings_cache",
        "dotmac_kernel.settings_crypto",
        "dotmac_kernel.source_applications",
        "dotmac_kernel.transactions",
        "dotmac_kernel.web_deps",
        "dotmac_kernel.web_surfaces",
    }
)

#: Ledger-allowlisted pure surfaces (app/-importable) plus the tests/-only
#: test kit. Every one must import with no database configured.
PURE_IMPORT_SURFACES = (
    "dotmac_kernel",
    "dotmac_kernel.assembly",
    "dotmac_kernel.capabilities",
    "dotmac_kernel.features",
    "dotmac_kernel.money",
    "dotmac_kernel.profiles",
    "dotmac_kernel.providers.provisioning",
    "dotmac_kernel.testing",
    "dotmac_kernel.testing.fakes",
    "dotmac_kernel.testing.provisioning",
    "dotmac_kernel.testing.licensing",
)

#: Sub's top-level route prefixes at import time (``app.main.app``). A kernel
#: mount would add e.g. ``/admin`` feature surfaces or platform auth routes;
#: any NEW first segment must be a reviewed change to this pin.
EXPECTED_ROUTE_PREFIXES = frozenset(
    {
        "/api",
        "/auth",
        "/docs",
        "/health",
        "/metrics",
        "/openapi.json",
        "/redoc",
        "/static",
        "/widget",
    }
)


def _no_db_env() -> dict[str, str]:
    """The current env with every database-ish variable stripped.

    If any kernel surface below read ``DATABASE_URL`` (or constructed an
    engine) at import time, its canary subprocess would fail — that is the
    point.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not (
            "DATABASE" in key
            or "SQLALCHEMY" in key
            or key.startswith(("POSTGRES", "PG", "MYSQL"))
            or key == "DB_URL"
        )
    }


@pytest.mark.parametrize("module", PURE_IMPORT_SURFACES)
def test_pure_surface_imports_without_database(module: str) -> None:
    """Each allowed surface imports in a clean subprocess with no DB env."""
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [sys.executable, "-c", f"import {module}"],
        env=_no_db_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"`import {module}` failed with DATABASE_URL/DB env stripped — the "
        f"surface is not pure-contract:\n{result.stderr}"
    )


def test_installed_kernel_is_the_reviewed_pin_and_ships_py_typed() -> None:
    import dotmac_kernel

    assert "site-packages" in Path(dotmac_kernel.__file__).parts
    assert dotmac_kernel.__version__ == KERNEL_PIN
    dist = importlib.metadata.distribution("dotmac-kernel")
    assert dist.version == KERNEL_PIN
    files = dist.files or []
    assert any(str(f).endswith("dotmac_kernel/py.typed") for f in files), (
        "dotmac-kernel wheel does not ship py.typed — typed-contract "
        "consumption (AGENTS.md typed-contract rule) would silently degrade"
    )


def test_pyproject_pins_kernel_exactly_from_the_named_index() -> None:
    """Reject any range for dotmac-kernel: an upgrade is a reviewed change.

    This is the CI gate against an unreviewed range upgrade — ``^``, ``~``,
    ``>=``, ``*``, or a bare name would let the lockfile move the kernel
    without a ledger amendment.
    """
    import tomllib

    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)

    exact = f"dotmac-kernel=={KERNEL_PIN}"
    main_deps = data["project"]["dependencies"]
    kernel_main = [
        d for d in main_deps if d.replace(" ", "").startswith("dotmac-kernel")
    ]
    assert kernel_main == [exact], (
        f"[project.dependencies] must pin exactly {exact!r}, found {kernel_main!r}"
    )

    dev_deps = data["dependency-groups"]["dev"]
    kernel_dev = [d for d in dev_deps if d.replace(" ", "").startswith("dotmac-kernel")]
    assert kernel_dev == [f"dotmac-kernel[testing]=={KERNEL_PIN}"], (
        "[dependency-groups].dev must pin the [testing] extra at exactly "
        f"{KERNEL_PIN}, found {kernel_dev!r}"
    )

    enrich = data["tool"]["poetry"]["dependencies"]["dotmac-kernel"]
    assert enrich == {"version": KERNEL_PIN, "source": KERNEL_SOURCE}, enrich
    dev_enrich = data["tool"]["poetry"]["group"]["dev"]["dependencies"]["dotmac-kernel"]
    assert dev_enrich["version"] == KERNEL_PIN, dev_enrich
    assert dev_enrich["source"] == KERNEL_SOURCE, dev_enrich

    sources = {s["name"]: s for s in data["tool"]["poetry"]["source"]}
    assert KERNEL_SOURCE in sources, "the private index source is not declared"
    forgejo = sources[KERNEL_SOURCE]
    assert forgejo["priority"] == "explicit", forgejo
    assert forgejo["url"].startswith("https://registry.dotmac.io/"), forgejo
    # No credential material belongs in pyproject — URL only.
    assert "@" not in forgejo["url"].split("//", 1)[1], (
        "index URL must not embed credentials"
    )


def test_lock_carries_the_reviewed_kernel_release_bytes() -> None:
    """The current pin resolves to the exact reviewed a105 lock artifacts."""
    import tomllib

    with (PROJECT_ROOT / "poetry.lock").open("rb") as lock_file:
        lock = tomllib.load(lock_file)
    packages = [
        package for package in lock["package"] if package["name"] == "dotmac-kernel"
    ]
    assert len(packages) == 1
    assert packages[0]["version"] == KERNEL_PIN
    hashes = {entry["file"]: entry["hash"] for entry in packages[0]["files"]}
    assert hashes[f"dotmac_kernel-{KERNEL_PIN}-py3-none-any.whl"] == (
        f"sha256:{KERNEL_WHEEL_SHA256}"
    )
    assert hashes[f"dotmac_kernel-{KERNEL_PIN}.tar.gz"] == (
        f"sha256:{KERNEL_SDIST_SHA256}"
    )


def test_money_is_exact_decimal_and_rejects_float() -> None:
    from dotmac_kernel.money import CurrencyMismatchError, Money, MoneyError, currency

    ngn = currency("NGN")
    a = Money.of("10.50", ngn)
    b = Money.of("0.25", ngn)
    assert a.amount == Decimal("10.50")
    assert a.add(b).amount == Decimal("10.75")
    assert a.subtract(b).amount == Decimal("10.25")
    assert Money.of("0.1", ngn).add(Money.of("0.2", ngn)).amount == Decimal("0.30")
    with pytest.raises(MoneyError):
        Money.of(10.5, ngn)  # type: ignore[arg-type] — float is the offence
    with pytest.raises(CurrencyMismatchError):
        a.add(Money.of("1.00", currency("USD")))


def test_provisioning_contract_types_construct_and_check_runs() -> None:
    from dotmac_kernel.cache import TenantScope
    from dotmac_kernel.providers.provisioning import (
        ApplyResult,
        ObserveResult,
        PlanResult,
        ProvisioningProvider,
        ProvisioningRequest,
        ProvisioningStatus,
    )
    from dotmac_kernel.testing.provisioning import (
        FakeProvisioningProvider,
        check_provisioning_provider_contract,
    )

    # a89 made `participant_code` and an explicit `Scope` REQUIRED — an
    # intentional pre-1.0 break, because an ambient/nullable scope and an
    # unowned provider identity are no longer valid inputs. Naming both here is
    # the point of a compatibility canary: it fails on the kernel bump rather
    # than in a caller that quietly kept provisioning without saying for whom.
    request = ProvisioningRequest(
        participant_code="participant.canary",
        scope=TenantScope(tenant_id=uuid4()),
        intent_id="intent-1",
        spec={"profile": "basic"},
    )
    plan = PlanResult(intent_id=request.intent_id, plan_hash="hash-1")
    assert plan.is_noop
    applied = ApplyResult(
        intent_id=request.intent_id,
        operation_id="op-1",
        plan_hash=plan.plan_hash,
        status=ProvisioningStatus.SUCCEEDED,
    )
    assert applied.is_terminal and applied.succeeded
    observed = ObserveResult(
        intent_id=request.intent_id,
        operation_id="op-1",
        status=ProvisioningStatus.SUCCEEDED,
    )
    assert observed.is_terminal
    assert isinstance(FakeProvisioningProvider(), ProvisioningProvider)
    # The reusable contract the S4 adapter must pass, proven runnable today.
    check_provisioning_provider_contract(FakeProvisioningProvider)


def test_composition_metadata_types_construct() -> None:
    from dotmac_kernel.assembly import ProductAssemblySpec
    from dotmac_kernel.capabilities import CapabilityCatalogue, DuplicateCapabilityError
    from dotmac_kernel.features import FeatureManifest
    from dotmac_kernel.profiles import DeploymentProfileSpec

    manifest = FeatureManifest(name="sub.demo", capabilities=("demo.use",))
    spec = ProductAssemblySpec(name="dotmac-sub", modules=(manifest,))
    assert spec.modules == (manifest,)

    catalogue = CapabilityCatalogue.from_manifests([manifest])
    assert catalogue.is_declared("demo.use")
    assert catalogue.owner("demo.use") == "sub.demo"
    with pytest.raises(DuplicateCapabilityError):
        CapabilityCatalogue.from_manifests(
            [manifest, FeatureManifest(name="sub.other", capabilities=("demo.use",))]
        )

    profile = DeploymentProfileSpec(
        code="dedicated-isp",
        version="1",
        required_modules=frozenset({"sub.demo"}),
        commercial_provider="none",
        provisioning_provider="sub-owned",
        identity_provider="sub-owned",
        telemetry_provider="sub-owned",
        update_provider="none",
        ingress_provider="nginx",
        dns_verification_provider="none",
        tls_provider="none",
        default_locale="en-NG",
        supported_locales=frozenset({"en-NG"}),
        allowed_currencies=frozenset({"NGN"}),
        legal_authority="NG",
        data_residency="NG",
    )
    assert profile.provider_selections()["provisioning"] == "sub-owned"


def test_fake_licence_signer_works_with_subs_cryptography_pin() -> None:
    """Sub pins cryptography==42.0.8 itself; a8's [testing] extra no longer
    forces a cryptography floor, and the signer works against Sub's pin."""
    assert importlib.metadata.version("cryptography") == "42.0.8"
    from dotmac_kernel.testing.licensing import FakeLicenceSigner

    signer = FakeLicenceSigner()
    assert signer.key().public_key_b64
    assert signer.keyring().get(signer.key_id) is not None


def test_app_import_graph_holds_only_allowlisted_kernel_modules() -> None:
    """The runtime graph agrees with the allowlist — no side-door import.

    This test asserted ZERO kernel modules until the settings cutover, and the
    change is deliberate rather than a relaxation to make a build pass.
    ``app/services/settings_spec.py`` now registers Sub's 560 specs with
    ``dotmac_kernel.settings_resolver`` at import time and resolves through it,
    so the count cannot be zero and Sub is no longer merely PINNED to the
    kernel — it consumes it. The ledger records the same fact.

    What the guard still proves is the thing that mattered all along: the AST
    check (``test_kernel_import_boundary``) proves no source-level import
    outside the allowlist exists, and this proves the RUNTIME graph agrees —
    an entry point, plugin, or transitive side door that dragged in
    ``dotmac_kernel.deps``, ``.middleware`` or ``.audit`` would still fail
    here, because those are what a "kernel-free Sub app" was ever about.

    Submodules of an allowlisted package are permitted: importing
    ``settings_resolver`` legitimately pulls ``dotmac_kernel.settings_models``
    and the value-type registry it validates against.

    ``TRANSITIVE_KERNEL_MODULES`` is the rest, and writing it down is the point
    — see that constant.
    """
    allowed = repr(sorted(ALLOWED_KERNEL_MODULES | TRANSITIVE_KERNEL_MODULES))
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                f"allowed = set({allowed})\n"
                "import app.main\n"
                "loaded = {m for m in sys.modules if m.startswith('dotmac_kernel')}\n"
                "bad = sorted(\n"
                "    m for m in loaded\n"
                "    if m != 'dotmac_kernel'\n"
                "    and not any(m == a or m.startswith(a + '.') for a in allowed)\n"
                ")\n"
                "sys.exit(\n"
                "    'kernel modules in the app import graph that the ledger "
                "allowlist does not admit: %s' % bad if bad else 0\n"
                ")"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def test_app_middleware_and_routes_are_kernel_free_and_prefixes_unchanged() -> None:
    """The unchanged Sub app: no kernel middleware, no kernel endpoints, and
    the top-level route prefix set is exactly the reviewed pin."""
    expected = repr(set(EXPECTED_ROUTE_PREFIXES))
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [
            sys.executable,
            "-c",
            (
                "from app.main import app\n"
                f"expected = {expected}\n"
                "for middleware in app.user_middleware:\n"
                "    module = middleware.cls.__module__\n"
                "    assert not module.startswith('dotmac_kernel'), middleware\n"
                "    dispatch = middleware.kwargs.get('dispatch')\n"
                "    if dispatch is not None:\n"
                "        module = dispatch.__module__\n"
                "        assert not module.startswith('dotmac_kernel'), middleware\n"
                "prefixes = set()\n"
                "for route in app.routes:\n"
                "    path = getattr(route, 'path', '')\n"
                "    segments = path.split('/')\n"
                "    prefixes.add('/' + segments[1] if len(segments) > 1 else path)\n"
                "    endpoint = getattr(route, 'endpoint', None)\n"
                "    if endpoint is not None:\n"
                "        module = getattr(endpoint, '__module__', '')\n"
                "        assert not module.startswith('dotmac_kernel'), (\n"
                "            f'kernel endpoint mounted at {path}'\n"
                "        )\n"
                "assert prefixes == expected, (\n"
                "    'top-level route prefixes changed with the kernel installed — '"
                "    'a new prefix must be a reviewed change to this pin:\\n'\n"
                "    f'added: {sorted(prefixes - expected)}\\n'\n"
                "    f'removed: {sorted(expected - prefixes)}'\n"
                ")\n"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def test_a105_sub_boot_does_not_construct_or_resolve_kernel_runtime() -> None:
    """K105 may load its runtime facility, but Sub must not use it at boot.

    The pre-import traps are sensitivity-planted before importing ``app``.
    They reject Kernel-attributed engine/session-factory construction and
    DatabaseRuntime construction, binding, reference claims or resolution,
    while allowing Sub's own ``app.db`` engine and session factory.

    Fixed phases and one stack-only dump precede the unchanged 120s deadline.
    Timeout reports read at most 32 KiB from a dedicated diagnostic stream;
    diagnostics never inspect local values or the process environment.
    """
    with TemporaryFile() as diagnostics:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import faulthandler\n"
                    "import sys\n"
                    "diagnostics = open(int(sys.argv[1]), 'w', closefd=False)\n"
                    "faulthandler.dump_traceback_later(90, repeat=False, file=diagnostics)\n"
                    "print('K105 boot probe: dependency imports', file=diagnostics, flush=True)\n"
                    "import sqlalchemy\n"
                    "import sqlalchemy.engine\n"
                    "import sqlalchemy.engine.create\n"
                    "import sqlalchemy.orm\n"
                    "import sqlalchemy.orm.session\n"
                    "print('K105 boot probe: sensitivity plants', file=diagnostics, flush=True)\n"
                    "kernel_engine_calls = []\n"
                    "kernel_sessionmaker_calls = []\n"
                    "kernel_runtime_operations = []\n"
                    "sub_engine_calls = []\n"
                    "sub_sessionmaker_calls = []\n"
                    "def caller_module():\n"
                    "    frame = sys._getframe(2)\n"
                    "    return frame.f_globals.get('__name__', '')\n"
                    "original_create_engine = sqlalchemy.create_engine\n"
                    "original_sessionmaker = sqlalchemy.orm.sessionmaker\n"
                    "def guarded_create_engine(*args, **kwargs):\n"
                    "    module = caller_module()\n"
                    "    if module.startswith('dotmac_kernel'):\n"
                    "        kernel_engine_calls.append(module)\n"
                    "        raise RuntimeError('Kernel engine construction is forbidden')\n"
                    "    if module == 'app.db':\n"
                    "        sub_engine_calls.append(module)\n"
                    "    return original_create_engine(*args, **kwargs)\n"
                    "def guarded_sessionmaker(*args, **kwargs):\n"
                    "    module = caller_module()\n"
                    "    if module.startswith('dotmac_kernel'):\n"
                    "        kernel_sessionmaker_calls.append(module)\n"
                    "        raise RuntimeError('Kernel sessionmaker construction is forbidden')\n"
                    "    if module == 'app.db':\n"
                    "        sub_sessionmaker_calls.append(module)\n"
                    "    return original_sessionmaker(*args, **kwargs)\n"
                    "sqlalchemy.create_engine = guarded_create_engine\n"
                    "sqlalchemy.engine.create_engine = guarded_create_engine\n"
                    "sqlalchemy.engine.create.create_engine = guarded_create_engine\n"
                    "sqlalchemy.orm.sessionmaker = guarded_sessionmaker\n"
                    "sqlalchemy.orm.session.sessionmaker = guarded_sessionmaker\n"
                    "runtime_operations = {\n"
                    "    '__init__', 'from_urls', 'bind_database_runtime',\n"
                    "    '_claim_reference_runtime_import', 'resolve_database_runtime',\n"
                    "}\n"
                    "def runtime_profile(frame, event, arg):\n"
                    "    if (event == 'call'\n"
                    "        and frame.f_globals.get('__name__') == 'dotmac_kernel.session_runtime'\n"
                    "        and frame.f_code.co_name in runtime_operations):\n"
                    "        kernel_runtime_operations.append(frame.f_code.co_name)\n"
                    "        raise RuntimeError('Kernel DatabaseRuntime operation is forbidden')\n"
                    "def expect_plant_rejected(call, message):\n"
                    "    try:\n"
                    "        call()\n"
                    "    except RuntimeError as exc:\n"
                    "        assert str(exc) == message, exc\n"
                    "    else:\n"
                    "        raise AssertionError('sensitivity plant escaped its trap')\n"
                    "kernel_db = {'__name__': 'dotmac_kernel.db', "
                    "'sqlalchemy': sqlalchemy}\n"
                    "exec(compile(\n"
                    "    \"def plant(): return sqlalchemy.create_engine('postgresql://unused')\",\n"
                    "    'dotmac_kernel/db.py', 'exec'), kernel_db)\n"
                    "expect_plant_rejected(kernel_db['plant'], "
                    "'Kernel engine construction is forbidden')\n"
                    "kernel_runtime = {'__name__': 'dotmac_kernel.session_runtime', "
                    "'sqlalchemy': sqlalchemy}\n"
                    "exec(compile(\n"
                    "    \"def engine_plant(): return sqlalchemy.create_engine('postgresql://unused')\\n\"\n"
                    '    "def factory_plant(): return sqlalchemy.orm.sessionmaker()\\n"\n'
                    '    "def resolve_database_runtime(): return None\\n"\n'
                    '    "def from_urls(): return None\\n"\n'
                    '    "def bind_database_runtime(): return None\\n"\n'
                    '    "def _claim_reference_runtime_import(): return None\\n"\n'
                    '    "class DatabaseRuntime:\\n    def __init__(self): return None\\n",\n'
                    "    'dotmac_kernel/session_runtime.py', 'exec'), kernel_runtime)\n"
                    "expect_plant_rejected(kernel_runtime['engine_plant'], "
                    "'Kernel engine construction is forbidden')\n"
                    "expect_plant_rejected(kernel_runtime['factory_plant'], "
                    "'Kernel sessionmaker construction is forbidden')\n"
                    "sys.setprofile(runtime_profile)\n"
                    "try:\n"
                    "    for name, args in (\n"
                    "        ('resolve_database_runtime', ()), ('from_urls', ()),\n"
                    "        ('bind_database_runtime', ()),\n"
                    "        ('_claim_reference_runtime_import', ()), ('DatabaseRuntime', ()),\n"
                    "    ):\n"
                    "        sys.setprofile(runtime_profile)\n"
                    "        expect_plant_rejected(lambda: kernel_runtime[name](*args),\n"
                    "            'Kernel DatabaseRuntime operation is forbidden')\n"
                    "finally:\n"
                    "    sys.setprofile(None)\n"
                    "kernel_engine_calls.clear()\n"
                    "kernel_sessionmaker_calls.clear()\n"
                    "kernel_runtime_operations.clear()\n"
                    "sub_engine_calls.clear()\n"
                    "sub_sessionmaker_calls.clear()\n"
                    "print('K105 boot probe: profiled app import', file=diagnostics, flush=True)\n"
                    "sys.setprofile(runtime_profile)\n"
                    "try:\n"
                    "    import app.main\n"
                    "finally:\n"
                    "    sys.setprofile(None)\n"
                    "    faulthandler.cancel_dump_traceback_later()\n"
                    "print('K105 boot probe: import complete; assertions', file=diagnostics, flush=True)\n"
                    "forbidden = {'dotmac_kernel.app_factory', 'dotmac_kernel.db', "
                    "'dotmac_kernel.database_runtime'}\n"
                    "assert forbidden.isdisjoint(sys.modules), forbidden & set(sys.modules)\n"
                    "assert not kernel_engine_calls, kernel_engine_calls\n"
                    "assert not kernel_sessionmaker_calls, kernel_sessionmaker_calls\n"
                    "assert not kernel_runtime_operations, kernel_runtime_operations\n"
                    "assert sub_engine_calls, 'Sub app.db engine construction was not observed'\n"
                    "assert sub_sessionmaker_calls, 'Sub app.db sessionmaker was not observed'\n"
                    "runtime = sys.modules.get('dotmac_kernel.session_runtime')\n"
                    "assert runtime is not None, 'K105 session_runtime was not observed'\n"
                    "assert runtime._binding is None, runtime._binding\n"
                    "assert runtime._reference_claimed is False, runtime._reference_claimed\n",
                    str(diagnostics.fileno()),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                pass_fds=(diagnostics.fileno(),),
            )
        except subprocess.TimeoutExpired:
            diagnostics.seek(0)
            trace = diagnostics.read(32 * 1024).decode("utf-8", errors="replace")
            raise AssertionError(
                "K105 boot probe exceeded the unchanged 120s deadline:\n" + trace
            ) from None
    assert result.returncode == 0, result.stderr
