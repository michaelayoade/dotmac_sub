"""No entry point may bind a credential to a string literal in source.

A credential in source is a credential in every clone, every image layer and
every fork, and no rotation reaches it. The case that produced this guard was
`scripts/one_off/send_reseller_welcome_email.py`: a module-level password
constant, one value shared by every recipient, interpolated into an email body
and sent to two dozen external organisations. Nothing imported the script; it
had run once and stayed. Deleting it removed the credential, a bulk recipient
list and a mistyped preview address in one move — this guard is what stops the
shape returning.

The detector (`tests/architecture/credential_literal.py`) matches SHAPE. It
holds no denylist and no sample of anything that leaked, because a scanner
that recognises a secret by its text has to store that secret to work, and a
guard written that way re-commits the thing it was added to remove.

`test_the_detector_fires_on_the_shape_it_exists_for` is the sensitivity proof
and is not optional: the scanned tree is clean apart from three reviewed
files, so every ratchet assertion here would pass over an almost-empty set
whether or not the detector still worked.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.migration_source.surfaces import COHORT_SURFACES
from tests.architecture.credential_literal import (
    EXCLUDED_LANES,
    SCANNED_ROOTS,
    binds_a_credential_literal,
    counts_by_path,
    literals_for,
    literals_in_source,
)
from tests.architecture.source_index import python_ast, string_constants

BASELINE = Path("tests/architecture/credential_literal_baseline.txt")

REVIEWED = "reviewed"
GRANDFATHERED = "grandfathered"

REMOVED_ONE_OFF = Path("scripts/one_off/send_reseller_welcome_email.py")
SCHEMA_CONTRACT_SCRIPT = Path(
    "scripts/migration/payment_prepaid_application_archive_schema.py"
)
FIXTURE_SEED_SCRIPT = Path("scripts/seed/seed_test_fixtures.py")
EDGE_CASE_DRIVER = Path("scripts/testing/drive_edge_cases.py")

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")

#: Python-bearing top-level directories this guard neither scans nor treats as
#: an excluded lane. Naming them keeps "unmonitored" honest: these are regions
#: the guard does not see, not regions it has cleared.
UNMONITORED_PYTHON_LANES = ("examples", "mobile")

#: A stand-in value used only to drive the detector. It is not a credential and
#: is not the value of anything.
PLACEHOLDER = "not-a-real-value"


def _value_node(expression: str) -> ast.expr:
    return ast.parse(expression, mode="eval").body


def _baseline() -> dict[str, tuple[int, str]]:
    allowed: dict[str, tuple[int, str]] = {}
    for raw in BASELINE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, count, classification = line.rsplit(" ", 2)
        assert classification in (REVIEWED, GRANDFATHERED), (
            f"{path}: unknown classification {classification!r} — a baseline "
            "entry is either reviewed-and-correct or grandfathered debt"
        )
        allowed[path] = (int(count), classification)
    return allowed


# ── the ratchet ──────────────────────────────────────────────────────────────


def test_no_unlisted_file_binds_a_credential_literal() -> None:
    allowed = _baseline()
    current = counts_by_path()

    unlisted = sorted(set(current) - set(allowed))
    assert not unlisted, (
        "These files bind a credential-named identifier to a string literal. "
        "Read it from the environment, a settings row or an installed secret "
        "source instead — and if the literal ever reached a real system, "
        "rotate it, because deleting the line does not recall it: "
        f"{unlisted}"
    )


def test_no_listed_file_grows_its_count() -> None:
    allowed = _baseline()
    current = counts_by_path()

    grew = sorted(
        f"{path}: {count} > {allowed[path][0]} allowed"
        for path, count in current.items()
        if count > allowed[path][0]
    )
    assert not grew, (
        "A file already carrying credential-shaped bindings gained another. "
        f"The allowance covers what was reviewed, not what comes next: {grew}"
    )


def test_the_baseline_shrinks_with_reality() -> None:
    """An allowance that outlives the thing it allowed is a silent hole."""

    current = counts_by_path()
    stale = sorted(
        f"{path} (allows {count}, now {current.get(path, 0)})"
        for path, (count, _) in _baseline().items()
        if current.get(path, 0) < count
    )
    assert not stale, (
        "These allowances are larger than reality — lower or remove them in "
        f"this change so the ratchet keeps its grip: {stale}"
    )


def test_grandfathered_entries_stay_distinct_from_reviewed_ones() -> None:
    """`reviewed` means correct. `grandfathered` means owed. Never merge them."""

    grandfathered = sorted(
        path
        for path, (_, classification) in _baseline().items()
        if classification == GRANDFATHERED
    )
    assert not grandfathered, (
        "Grandfathered credential literals are outstanding debt, listed here "
        f"so they are never mistaken for reviewed: {grandfathered}"
    )


# ── sensitivity proof ────────────────────────────────────────────────────────


def test_the_detector_fires_on_the_shape_it_exists_for() -> None:
    """Construct the removed script's shape and prove the detector sees it.

    Every ratchet assertion above runs over a near-empty set. If the detector
    silently stopped matching, they would all still pass. This is the test
    that fails instead.
    """

    offending = "\n".join(
        (
            f"TEMPORARY_PASSWORD = {PLACEHOLDER!r}",
            f"API_KEY: str = {PLACEHOLDER!r}",
            f"client.secret = {PLACEHOLDER!r}",
            f"SECRET_KEY, RETRIES = {PLACEHOLDER!r}, 3",
            f"create_user(name='x', password={PLACEHOLDER!r})",
            f"PRIVATE_KEY = f{PLACEHOLDER!r}",
        )
    )
    seen = {hit.identifier for hit in literals_in_source(offending, "constructed.py")}
    assert seen == {
        "TEMPORARY_PASSWORD",
        "API_KEY",
        "secret",
        "SECRET_KEY",
        "password",
        "PRIVATE_KEY",
    }, f"the detector missed a credential-binding shape; it saw {sorted(seen)}"


def test_the_detector_ignores_resolved_and_named_credentials() -> None:
    """The distinction the guard turns on: a call is not a literal.

    If these matched, the guard would be noise and someone would delete it.
    """

    innocent = "\n".join(
        (
            "PASSWORD = os.environ['APP_PASSWORD']",
            "API_KEY = settings.api_key",
            "TOKEN = require_secret('jwt_secret')",
            "PASSWORD = ''",
            "SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')",
            "PASSWORD, RETRIES = load_pair()",
            "PASSWORD_RESET_COOKIE = 'password_reset'",
            "token_type = 'bearer_token_type'",
            "CREDENTIALS = 'user_credentials'",
            "api_key = 'api_key'",
            "PASSWORD = f'{prefix}-suffix'",
        )
    )
    hits = literals_in_source(innocent, "constructed.py")
    assert not hits, f"the detector reported a non-credential: {[str(h) for h in hits]}"


def test_the_credential_word_must_end_the_identifier() -> None:
    """`x_password` holds one; `password_x` names something about one."""

    literal = _value_node(repr(PLACEHOLDER))
    assert binds_a_credential_literal("vendor_password", literal)
    assert not binds_a_credential_literal("password_field_label", literal)


# ── the premises that make the reviewed entries exemptions ───────────────────


def test_the_removed_one_off_has_not_returned() -> None:
    """The originating file, by path. It was dead code and stays deleted."""

    assert not REMOVED_ONE_OFF.exists(), (
        f"{REMOVED_ONE_OFF} is back. It mailed one shared password to a "
        "hard-coded external recipient list; it must not be restored."
    )


def test_schema_contract_default_tokens_are_sql_defaults_not_credentials() -> None:
    """Premise: every hit there is a `default_token=` on a local ColumnContract.

    `default_token` carries a SQL DEFAULT expression, so the word "token" is
    lexical rather than a bearer credential. If the hits in this file stop
    being that argument to that locally defined class, the exemption is void.
    """

    hits = literals_for(SCHEMA_CONTRACT_SCRIPT)
    assert hits, "the reviewed file no longer offends — drop its baseline line"
    assert {hit.identifier for hit in hits} == {"default_token"}, (
        "a different credential-shaped binding appeared in "
        f"{SCHEMA_CONTRACT_SCRIPT}: {[str(hit) for hit in hits]}"
    )

    tree = python_ast(SCHEMA_CONTRACT_SCRIPT)
    defined_locally = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    assert "ColumnContract" in defined_locally, (
        "ColumnContract is no longer defined in this module, so the premise "
        "that `default_token` is its SQL DEFAULT field cannot be checked here"
    )
    receivers = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and any(keyword.arg == "default_token" for keyword in node.keywords)
    }
    assert receivers == {"ColumnContract"}, (
        f"`default_token` is now passed to something else: {sorted(receivers)}"
    )


def test_the_fixture_seed_is_still_classified_non_production() -> None:
    """Premise: the fixture password authenticates against disposable data.

    The repository's own source inventory is the authority for that, not this
    test's opinion. If the seeder is ever reclassified as production-reachable,
    its shared literal stops being inert and this exemption fails first.
    """

    surface = next(
        (s for s in COHORT_SURFACES if s.path == FIXTURE_SEED_SCRIPT.as_posix()),
        None,
    )
    assert surface is not None, (
        f"{FIXTURE_SEED_SCRIPT} left the source inventory, so nothing "
        "authoritative still says its credential is non-production"
    )
    assert not surface.production_runtime, (
        f"{FIXTURE_SEED_SCRIPT} is now production-reachable; its shared "
        "fixture password must be removed rather than exempted"
    )


def test_the_edge_case_driver_only_targets_loopback() -> None:
    """Premise: the driver signs in to a locally started test app.

    The credential is the fixture seeder's, and it only ever travels to
    127.0.0.1. Repointing this script at a real host is exactly the change
    that must fail here.
    """

    urls = sorted(
        value
        for value in string_constants(EDGE_CASE_DRIVER)
        if value.startswith(("http://", "https://"))
    )
    assert urls, f"{EDGE_CASE_DRIVER} no longer names a target host"
    offsite = [
        url for url in urls if not url.split("//", 1)[1].startswith(LOOPBACK_HOSTS)
    ]
    assert not offsite, (
        f"{EDGE_CASE_DRIVER} now drives a non-loopback host while holding a "
        f"password literal: {offsite}"
    )


# ── scope ────────────────────────────────────────────────────────────────────


def test_every_python_entry_point_family_is_scanned() -> None:
    """Guards cover families, not one directory.

    Application code with its Celery tasks and workers, operator CLI, seeds
    and one-off scripts, and schema migrations. A new Python-bearing top-level
    surface must be classified here rather than discovered later.
    """

    assert set(SCANNED_ROOTS) == {"app", "scripts", "alembic"}
    for root in SCANNED_ROOTS:
        assert Path(root).is_dir(), f"scanned root {root} no longer exists"
    assert Path("app/tasks").is_dir(), "the task/worker family moved out of app/"

    classified = (
        set(SCANNED_ROOTS) | set(EXCLUDED_LANES) | set(UNMONITORED_PYTHON_LANES)
    )
    unclassified = sorted(
        entry.name
        for entry in Path().iterdir()
        if entry.is_dir()
        and not entry.name.startswith(".")
        and entry.name not in classified
        and entry.name not in {"node_modules", "venv", "site-packages"}
        and any(entry.rglob("*.py"))
    )
    assert not unclassified, (
        "a Python-bearing top-level directory appeared that this guard "
        "neither scans, excludes, nor names as unmonitored: "
        f"{unclassified}"
    )
