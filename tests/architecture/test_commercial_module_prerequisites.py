"""Deployment guardrails for composed commercial module prerequisites."""

from __future__ import annotations

import ast
import configparser
import importlib.util
import re
import tomllib
from pathlib import Path

from app.commercial_module_prereqs import module_schema_contract
from app.migration_schema_ops import declared_idempotent_schema_create_target

ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = ROOT / "alembic.ini"
MIGRATION_546 = ROOT / "alembic" / "versions" / "546_module_db_roles_prereq.py"
DEPLOY = ROOT / "scripts" / "deploy.sh"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_commercial_module_prereqs.py"


def _executed_sql(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    statements: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "execute"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                statements.append(argument.value)
            elif isinstance(argument, ast.JoinedStr):
                statements.append(ast.unparse(argument))
    return "\n".join(statements)


def _declared_lineages() -> tuple[str, ...]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ALEMBIC_INI)
    entries = parser["alembic"]["version_locations"].split()
    return tuple(
        entry.removesuffix(".migrations:versions")
        for entry in entries
        if entry.endswith(".migrations:versions")
    )


def test_the_schema_set_is_derived_rather_than_restated() -> None:
    """Derivation is the guard, so prove the chain is intact end to end.

    The contract used to be a hand-written tuple asserted equal to
    `alembic.ini`. That caught a missing entry but not a missing *environment*:
    the tuple and the five prose lists still had to be edited by hand, and on
    2026-08-31 `mod_inbox` was in the tuple and in none of the prose.
    """
    derived = module_schema_contract()
    assert {item.import_name for item in derived} == set(_declared_lineages())
    assert derived, "the derivation must not silently produce an empty contract"

    for item in derived:
        assert item.schema.startswith("mod_"), item.schema
        assert item.owner_role == "dotmac_app"
        assert item.usage_roles == ("app_admin", "app_user", "platform_api")

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        requirement.split("==")[0]
        for requirement in data["project"]["dependencies"]
        if requirement.startswith("dotmac-")
    }
    assert {item.distribution for item in derived} <= dependencies


def test_the_rendered_schema_document_is_the_only_list() -> None:
    """One derived document, and the prose must not grow a rival.

    `make schema-contract-check` is the byte comparison; this is the guard that
    the documents which used to carry their own lists now point at it instead.

    The premise is deliberately about ENUMERATIONS, not mentions. Three of these
    documents are dated historical records, and a sentence like "installed under
    `mod_billing` and `mod_coll`" is a statement about two specific modules, not
    a copy of the required set — rewriting that would be falsifying history to
    satisfy a checker. What went stale was the five-item list, restated in three
    places and edited by hand. So: no line may name three or more module
    schemas, which is what a list looks like and what contextual prose does not.
    """
    schemas = {item.schema for item in module_schema_contract()}

    rendered_path = ROOT / "docs" / "generated" / "MODULE_SCHEMA_CONTRACT.md"
    rendered = rendered_path.read_text(encoding="utf-8")
    for schema in schemas:
        assert f"`{schema}`" in rendered

    offenders: list[str] = []
    for path in sorted(ROOT.joinpath("docs").rglob("*.md")):
        if path == rendered_path:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            named = sorted(schema for schema in schemas if schema in line)
            if len(named) >= 3:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{number} enumerates "
                    f"{', '.join(named)}"
                )

    assert not offenders, (
        "these lines keep a parallel copy of the module schema set; the derived "
        "list lives in docs/generated/MODULE_SCHEMA_CONTRACT.md and documents "
        "must point at it:\n  " + "\n  ".join(offenders)
    )

    # Sensitivity: the detector must be able to fire, or it proves nothing.
    fabricated = " ".join(sorted(schemas)[:3])
    assert len([s for s in schemas if s in fabricated]) >= 3


def _module_lineage_migrations() -> list[Path]:
    """Every migration file in every composed module lineage."""
    files: list[Path] = []
    for import_name in _declared_lineages():
        spec = importlib.util.find_spec(f"{import_name}.migrations")
        if spec is None or not spec.submodule_search_locations:
            continue
        versions = Path(list(spec.submodule_search_locations)[0]) / "versions"
        if versions.is_dir():
            files.extend(sorted(versions.glob("*.py")))
    return files


def test_sub_migrations_never_create_a_schema() -> None:
    """Schema creation is a deployment prerequisite, not a migration effect.

    Sub's own lineage runs as the restricted migration role, which deliberately
    has no database-level CREATE. A `CREATE SCHEMA` here could only work by
    someone having granted that privilege, which is the thing ADR-0011 forbids.
    """
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        statements = _executed_sql(path).upper()
        assert "CREATE SCHEMA" not in statements, (
            f"{path.relative_to(ROOT).as_posix()} emits CREATE SCHEMA from "
            "Alembic; module schemas belong to "
            "scripts/bootstrap_commercial_module_prereqs.py."
        )


def test_every_module_lineage_schema_create_is_intercepted() -> None:
    """The composed lineages DO ship `CREATE SCHEMA`; that is upstream's right.

    Sub cannot edit an exact-pinned third-party lineage, and would not want to:
    the same distribution has to install under a product that does grant its
    migration role CREATE. What Sub owns is the interception. Every such
    statement must be one `declared_idempotent_schema_create_target` recognises,
    because anything it does not recognise reaches the database verbatim and
    fails the deploy as a permission error.

    This is the guard the repo lacked: `CREATE ROLE` was forbidden in Sub's own
    migrations and nothing at all looked at the package lineages, which are
    where the real second creator lives.
    """
    inspected = 0
    for path in _module_lineage_migrations():
        for statement in _executed_sql(path).splitlines():
            if "CREATE SCHEMA" not in statement.upper():
                continue
            inspected += 1
            target = declared_idempotent_schema_create_target(statement.strip())
            assert target is not None, (
                f"{path.name} emits an unrecognised schema create "
                f"({statement.strip()!r}); app/migration_schema_ops.py would "
                "let it through to the restricted migration role."
            )
            assert target in {item.schema for item in module_schema_contract()}

    # Sensitivity: a guard over an empty set passes for the wrong reason.
    assert inspected > 0, (
        "no module lineage CREATE SCHEMA statements were found; either the "
        "lineages are not installed or the scan stopped working"
    )


def test_cluster_role_creation_is_owned_by_the_bootstrap_script() -> None:
    bootstrap_source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "CREATE ROLE" in bootstrap_source
    assert 'sql.SQL("CREATE ROLE {} {}")' in bootstrap_source
    assert 'sql.SQL("ALTER ROLE {} {}")' in bootstrap_source
    assert "BOOTSTRAP_DATABASE_URL" in bootstrap_source
    assert "MIGRATION_DATABASE_URL" in bootstrap_source

    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        sql = _executed_sql(path).upper()
        assert "CREATE ROLE" not in sql, (
            f"{path.relative_to(ROOT).as_posix()} emits CREATE ROLE from "
            "Alembic; cluster identities belong to the explicit bootstrap."
        )
        assert "ALTER ROLE" not in sql, (
            f"{path.relative_to(ROOT).as_posix()} emits ALTER ROLE from "
            "Alembic; cluster identities belong to the explicit bootstrap."
        )


def test_546_verifies_module_roles_instead_of_creating_them() -> None:
    source = MIGRATION_546.read_text(encoding="utf-8")
    assert "module_database_role_violations" in source
    assert "_assert_module_database_roles_exist()" in source
    assert "CREATE ROLE" not in _executed_sql(MIGRATION_546)
    assert "scripts/bootstrap_commercial_module_prereqs.py" in source


def test_deploy_preflights_prerequisites_before_backup_and_alembic() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "run_database_prerequisite_bootstrap" in deploy
    assert "verify_database_prerequisites" in deploy
    assert "scripts/bootstrap_commercial_module_prereqs.py --repair" in deploy
    assert "scripts/bootstrap_outbox_dispatcher_roles.py --repair" in deploy
    assert "scripts/bootstrap_commercial_module_prereqs.py --verify-only" in deploy
    assert "scripts/bootstrap_outbox_dispatcher_roles.py --verify-only" in deploy

    verify_call = re.search(r"^verify_database_prerequisites$", deploy, re.MULTILINE)
    assert verify_call is not None
    assert verify_call.start() < deploy.index("Backing up database before migrations")
    assert verify_call.start() < deploy.index(
        'log "Applying migrations (alembic upgrade heads)"'
    )


def test_the_prerequisite_leg_reports_a_typed_outcome() -> None:
    """`already_satisfied`, `repaired` and `blocked` must all be reachable words.

    The defect: the old leg returned 0 both when there was nothing to do and
    when nothing could be done. Three named outcomes are the fix, so the three
    names have to actually exist in the deploy owner and the bootstrap.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")

    for outcome in ("already_satisfied", "repaired", "blocked"):
        assert outcome in deploy, f"deploy.sh never reports {outcome}"
        assert outcome in bootstrap, f"the bootstrap never reports {outcome}"

    assert "PREREQUISITE_OUTCOME" in deploy
    assert "DEPLOY RECEIPT:" in deploy


def test_the_prerequisite_leg_refuses_instead_of_returning_success() -> None:
    """The exact regression guard.

    `return 0` on a missing credential is what let two candidates and a
    production host reach a verification step that could not pass. A blocked
    repair must terminate the deploy.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    leg = deploy[
        deploy.index("run_database_prerequisite_bootstrap() {") : deploy.index(
            "verify_database_prerequisites() {"
        )
    ]
    assert "DEPLOY REFUSED" in leg
    assert "exit 1" in leg
    assert "No BOOTSTRAP_DATABASE_URL supplied" not in leg, (
        "the silent short-circuit is back"
    )

    blocked_at = leg.index('PREREQUISITE_OUTCOME="blocked"')
    assert leg.index("exit 1", blocked_at) > blocked_at


def test_the_deployment_never_reaches_for_the_application_password() -> None:
    """The 2026-08-31 production failure, as a guard.

    The repair connected as `postgres` using `.env`'s `POSTGRES_PASSWORD`,
    which is the APPLICATION password; the container's superuser password comes
    from `PG_LOCAL_BOOTSTRAP_PASSWORD`. Staging passed only because its two
    values happened to be equal. Nothing on a bootstrap path may read either:
    the credential is held in a pgpass file and read by libpq.
    """
    surfaces = {
        "scripts/deploy.sh": DEPLOY,
        ".github/workflows/temporary-module-prereq-repair.yml": (
            ROOT / ".github" / "workflows" / "temporary-module-prereq-repair.yml"
        ),
    }
    for label, path in surfaces.items():
        text = path.read_text(encoding="utf-8")
        assert "POSTGRES_PASSWORD" not in text, (
            f"{label} reads POSTGRES_PASSWORD; the schema bootstrap credential "
            "is a pgpass file, and POSTGRES_PASSWORD is the application's."
        )
        assert "PG_LOCAL_BOOTSTRAP_PASSWORD" not in text, (
            f"{label} reads a superuser password directly; the deployment holds "
            "a dedicated least-privilege credential instead."
        )


def test_the_repair_workflow_no_longer_uses_the_unproved_socket_path() -> None:
    """PR #2843's assumption, measured false on the production host.

    `/var/run` is a symlink to `/run` inside the alpine image, and the host
    kernel resolves that absolute target against the HOST root, so
    `/proc/<pid>/root/var/run/postgresql` cannot exist. The step was a bare
    `test -S` with no diagnostics, so it failed silently.
    """
    workflow = (
        ROOT / ".github" / "workflows" / "temporary-module-prereq-repair.yml"
    ).read_text(encoding="utf-8")
    assert "/proc/" not in workflow
    assert "s.PGSQL" not in workflow
    assert "dotmac_pg_local" not in workflow
    # And it must not be usable against production at all.
    assert "REFUSED" in workflow


def test_the_bootstrap_separates_the_two_credentials() -> None:
    """One mode creates roles, the other cannot, and they are different jobs."""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert "--repair-schemas" in bootstrap
    assert "allow_role_creation" in bootstrap
    assert "NOCREATEROLE" in bootstrap
    assert "EXIT_BLOCKED = 3" in bootstrap


def test_an_elevated_dsn_may_not_be_persisted_in_the_deploy_env() -> None:
    """Standing privilege must not be one edited line away.

    `env_value` greps the deploy directory's `.env`, so
    `${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}` made every
    deploy auto-repairing the moment anyone wrote that key into the file. The
    file being empty was the entire safety property and nothing enforced it.

    Note this is not satisfied by the leg merely *mentioning* `env_value`: it
    must read the key in order to REFUSE it. Governance ADR 0028's warning
    applies — presence of a step is not evidence the step does anything.
    """
    deploy = DEPLOY.read_text(encoding="utf-8")
    leg = deploy[
        deploy.index("run_database_prerequisite_bootstrap() {") : deploy.index(
            "verify_database_prerequisites() {"
        )
    ]

    assert (
        "${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}" not in leg
    ), "a .env-persisted elevated DSN would arm auto-repair on every deploy"
    assert 'persisted_url="$(env_value BOOTSTRAP_DATABASE_URL)"' in leg
    refusal_at = leg.index('persisted_url="$(env_value BOOTSTRAP_DATABASE_URL)"')
    assert "DEPLOY REFUSED" in leg[refusal_at:]
    assert "exit 1" in leg[refusal_at:]
    # The operator path survives, from the process environment only.
    assert 'bootstrap_url="${BOOTSTRAP_DATABASE_URL:-}"' in leg
