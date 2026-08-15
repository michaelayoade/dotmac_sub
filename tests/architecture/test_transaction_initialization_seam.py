"""No module may set transaction characteristics with raw SQL.

`SET TRANSACTION ISOLATION LEVEL ...` is only legal as the FIRST statement in a
transaction. Since #2353 installs `app.current_tenant` via `set_config` on
`after_begin`, it never is: a statement has always run before any caller SQL
arrives. Twelve read-only operator reports and one serializable writer raised
ActiveSqlTransaction on every PostgreSQL invocation, while SQLite unit coverage
stayed green because it skips the dialect branch entirely.

The repair is two seams in the transaction-authority layer. This guard stops the
raw pattern coming back — including through a typo, which is how one of the
twelve escaped the original search (`REPEATABLE READ READ ONLY`, no comma).
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts")

#: Deliberately loose. It matches the statement's opening, not one exact
#: spelling, so a comma, casing or whitespace variant cannot slip through.
RAW_SET_TRANSACTION = re.compile(
    r"SET\s+TRANSACTION\s+ISOLATION\s+LEVEL", re.IGNORECASE
)

#: `app/db.py` owns both seams and documents the defect in prose, so its
#: mentions are the definition of the rule rather than a violation of it.
ALLOWED = {Path("app/db.py")}


def _offenders(roots=SCANNED_ROOTS) -> dict[Path, int]:
    found: dict[Path, int] = {}
    for root in roots:
        for path in root.rglob("*.py"):
            relative = path.relative_to(PROJECT_ROOT)
            if relative in ALLOWED:
                continue
            source = path.read_text(encoding="utf-8")
            hits = len(RAW_SET_TRANSACTION.findall(source))
            if hits:
                found[relative] = hits
    return found


def test_no_module_sets_transaction_characteristics_with_raw_sql() -> None:
    offenders = _offenders()

    assert offenders == {}, (
        "use app.db.read_only_snapshot_session (reports) or "
        "app.db.begin_serializable_write (writers) instead of raw SQL; "
        "SET TRANSACTION cannot be the first statement while the "
        f"operator-tenant hook is installed: {offenders}"
    )


def test_the_guard_detects_a_planted_violation(tmp_path: Path) -> None:
    """Sensitivity proof: a guard that cannot fail is not a guard.

    Without this, deleting the pattern from the detector — or scanning a
    directory the offenders do not live in — would look exactly like success.
    """

    planted = tmp_path / "app"
    planted.mkdir()
    (planted / "offender.py").write_text(
        'db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))\n',
        encoding="utf-8",
    )

    assert _offenders(roots=(planted,))


def test_the_guard_catches_the_typo_variant(tmp_path: Path) -> None:
    """The spelling that escaped the original exact-string search.

    `crm_network_map_point_migration.py` carried
    `REPEATABLE READ READ ONLY` — no comma — and was equally broken while
    being invisible to a literal grep.
    """

    planted = tmp_path / "scripts"
    planted.mkdir()
    (planted / "typo.py").write_text(
        'db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))\n',
        encoding="utf-8",
    )

    assert _offenders(roots=(planted,))
