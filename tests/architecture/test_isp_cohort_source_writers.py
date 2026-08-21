"""Two-directional ratchet on the cohort-isp-01 source writer surface.

Governance cohort 1 — party, customers and brand profiles — will eventually
move to `asm-dotmac-isp`. Before that can be considered, the set of Sub code
that writes those facts has to be known and has to stop growing. This guard
does exactly that and nothing more: it records today's writers and fails on
movement in either direction.

Both directions matter, for different reasons.

*Upward* is the obvious one: a new writer added while the cutover is being
prepared is a writer nobody planned to displace, and it will be discovered
after the authority switch rather than before.

*Downward* matters because a ratchet that silently absorbs removals can be
spent twice. If a writer disappears and the baseline still claims it, the
budget for "one more writer" is quietly restored, and the next addition passes
a guard that should have caught it. So a removal is a failure until the same
change lowers the line — which is the pull request that actually retired the
writer, after the sealed switch, never in advance.

## Sensitivity

Every rejection test here is paired with a proof that the same code path
admits the legal case, and the whole-repository sweeps assert against a
*constructed* violation in a synthetic tree rather than only over today's
data. A guard that has only ever seen conforming input passes for the wrong
reason: it would keep passing if the detector it exercises were deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.migration_source import surfaces
from scripts.architecture.isp_cohort_writers import (
    EntryPointFamily,
    cohort_reference_sites,
    cohort_write_counts,
    cohort_write_sites,
    family_for,
    reference_counts_by_family,
    unscanned_python_roots,
)

BASELINE = Path(__file__).with_name("isp_cohort1_writer_baseline.txt")

REMEDY = (
    "cohort-isp-01 source state is frozen while the ISP replacement is "
    "prepared. Route the write through the owner named in "
    "app/migration_source/surfaces.py, or — if the write is genuinely new "
    "behaviour Sub still needs — say so in the pull request and raise this "
    "baseline deliberately. Regenerate with "
    "`python -m scripts.architecture.isp_cohort_writers --baseline`."
)


def _read_baseline(path: Path) -> dict[str, int]:
    """Read `count family|path` entries from the ratchet baseline."""

    counts: dict[str, int] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count, _, name = stripped.partition(" ")
        try:
            parsed = int(count)
        except ValueError as exc:  # pragma: no cover - malformed baseline
            raise ValueError(
                f"invalid baseline entry {path}:{line_number}: {raw!r}"
            ) from exc
        if parsed < 1:  # pragma: no cover - malformed baseline
            raise ValueError(f"baseline count must be positive at {path}:{line_number}")
        if "|" not in name:  # pragma: no cover - malformed baseline
            raise ValueError(
                f"baseline entry {path}:{line_number} is missing its "
                "`family|path` key; a bare path would let a writer move "
                "between families unnoticed"
            )
        counts[name.strip()] = parsed
    return counts


# --------------------------------------------------------------------------
# the ratchet
# --------------------------------------------------------------------------


def test_no_new_cohort_source_writer() -> None:
    current = cohort_write_counts()
    baseline = _read_baseline(BASELINE)

    added = sorted(set(current) - set(baseline))
    assert not added, (
        "new writers of cohort-isp-01 source state, absent from the "
        f"baseline. {REMEDY}\n  " + "\n  ".join(added)
    )

    grew = sorted(
        f"{name}: {baseline[name]} -> {current[name]}"
        for name in set(current) & set(baseline)
        if current[name] > baseline[name]
    )
    assert not grew, (
        f"existing cohort writers gained write sites. {REMEDY}\n  " + "\n  ".join(grew)
    )


def test_cohort_writer_baseline_is_not_stale() -> None:
    current = cohort_write_counts()
    baseline = _read_baseline(BASELINE)

    retired = sorted(set(baseline) - set(current))
    assert not retired, (
        "these writers no longer exist but the baseline still claims them. "
        "Remove the lines in the same change that removed the writers, or the "
        "ratchet silently regains room for a new one:\n  " + "\n  ".join(retired)
    )

    shrunk = sorted(
        f"{name}: baseline {baseline[name]}, now {current[name]}"
        for name in set(current) & set(baseline)
        if current[name] < baseline[name]
    )
    assert not shrunk, (
        "write sites were removed without lowering the baseline; a ratchet "
        "that absorbs removals can be spent twice:\n  " + "\n  ".join(shrunk)
    )


def test_every_entry_point_family_is_scanned() -> None:
    """No Python-bearing repository root escapes the census unexplained."""

    unscanned = unscanned_python_roots()
    assert not unscanned, (
        "these repository roots contain Python that the cohort writer census "
        "neither scans nor excuses. A guard scoped to the directories that "
        "happened to exist when it was written states an unenforceable "
        "premise. Add the root to SCAN_ROOTS, or to EXCLUDED_PYTHON_ROOTS "
        "with the reason it is not a writer surface:\n  " + "\n  ".join(unscanned)
    )


# --------------------------------------------------------------------------
# the inventory and the census must describe the same world
# --------------------------------------------------------------------------


def test_every_counted_writer_is_classified() -> None:
    counted = {site.path for site in cohort_write_sites()}
    classified = surfaces.inventoried_paths()

    unclassified = sorted(counted - classified)
    assert not unclassified, (
        "the census counts these as writers of cohort-isp-01 state, and "
        "app/migration_source/surfaces.py does not say what they are. An "
        "unclassified writer is an unowned one:\n  " + "\n  ".join(unclassified)
    )


def test_every_classified_writer_is_counted() -> None:
    counted = {site.path for site in cohort_write_sites()}
    claimed = {surface.path for surface in surfaces.COHORT_SURFACES if surface.writes}

    phantom = sorted(claimed - counted)
    assert not phantom, (
        "the inventory classifies these as writing cohort state, and the "
        "census cannot see the write. Either the write was removed and the "
        "classification is stale, or the detector has a gap worth naming:\n  "
        + "\n  ".join(phantom)
    )


def test_non_writing_surfaces_write_nothing() -> None:
    counted = {site.path for site in cohort_write_sites()}
    non_writers = {
        surface.path for surface in surfaces.COHORT_SURFACES if not surface.writes
    }

    contradicted = sorted(non_writers & counted)
    assert not contradicted, (
        "these are inventoried as adapters or read-only consumers, and the "
        "census counts them writing cohort state. An adapter that writes is "
        "the exact bypass this cohort has to find before cutover:\n  "
        + "\n  ".join(contradicted)
    )


def test_inventory_and_census_agree_on_family_names() -> None:
    """The duplicated family enum in `app/` must match the census's."""

    assert {member.value for member in surfaces.EntryPointFamily} == {
        member.value for member in EntryPointFamily
    }, (
        "app/migration_source/surfaces.py restates the census families "
        "because app/ may not import scripts/. They have drifted, so the "
        "inventory can now name a family the census cannot produce."
    )


def test_inventory_family_matches_the_census_family() -> None:
    census = {site.path: site.family for site in cohort_write_sites()}
    disagreements = sorted(
        f"{surface.path}: inventory {surface.family}, census {census[surface.path]}"
        for surface in surfaces.COHORT_SURFACES
        if surface.path in census and census[surface.path] != surface.family.value
    )
    assert not disagreements, (
        "the inventory and the census disagree about which entry-point family "
        "a writer belongs to:\n  " + "\n  ".join(disagreements)
    )


# --------------------------------------------------------------------------
# sensitivity: the detector must actually detect
# --------------------------------------------------------------------------


def _synthetic_tree(root: Path, relative: str, source: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def _counts(root: Path) -> dict[str, int]:
    return {site.key: site.count for site in cohort_write_sites(project_root=root)}


def test_detector_sees_a_new_direct_writer(tmp_path: Path) -> None:
    """A newly constructed cohort row in a service is counted."""

    _synthetic_tree(
        tmp_path,
        "app/services/brand_new_owner.py",
        "from app.models.subscriber import Subscriber\n"
        "def create(db):\n"
        "    row = Subscriber(first_name='a', last_name='b', email='c')\n"
        "    db.add(row)\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("service|app/services/brand_new_owner.py") == 1


def test_detector_sees_a_webhook_handler_writing_cohort_state(
    tmp_path: Path,
) -> None:
    """An inbound callback that writes the cohort is counted as its own family.

    A foreign system triggering a write to party or customer state is the
    hardest writer to notice by reading code, because nothing in this
    repository calls it.
    """

    _synthetic_tree(
        tmp_path,
        "app/api/partner_webhooks.py",
        "from app.models.party import Party\n"
        "def receive(db, payload):\n"
        "    db.add(Party(party_type='person', display_name=payload.name))\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("webhook_handler|app/api/partner_webhooks.py") == 1


def test_detector_sees_an_adapter_bypassing_its_owner(tmp_path: Path) -> None:
    """A route that mutates a fetched cohort row is counted as a writer.

    This is the shape that matters most: the adapter still *calls* a service,
    so a guard that only looked for `Model(...)` would pass it, and it still
    writes the account row afterwards.
    """

    _synthetic_tree(
        tmp_path,
        "app/api/customers.py",
        "from app.models.subscriber import Subscriber\n"
        "def patch(db, customer_id, payload):\n"
        "    subscriber = db.get(Subscriber, customer_id)\n"
        "    subscriber.billing_mode = payload.billing_mode\n"
        "    db.commit()\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("api_route|app/api/customers.py") == 1


def test_detector_sees_raw_sql_against_a_cohort_table(tmp_path: Path) -> None:
    _synthetic_tree(
        tmp_path,
        "scripts/one_off/repair.py",
        "SQL = 'UPDATE subscribers SET billing_mode = :mode WHERE id = :id'\n",
    )
    counts = _counts(tmp_path)
    assert counts.get("cli_script|scripts/one_off/repair.py") == 1


def test_detector_ignores_a_same_named_attribute_on_another_entity(
    tmp_path: Path,
) -> None:
    """The acceptance half: an unrelated `.status = ...` is not a cohort write.

    Without this, the guard would pass for the wrong reason — a detector that
    counted every `status` assignment in the repository would show a baseline
    so noisy nobody could act on it, and the first response would be to
    suppress the guard rather than the writer.
    """

    _synthetic_tree(
        tmp_path,
        "app/services/unrelated.py",
        "from app.models.support import Ticket\n"
        "def close(db, ticket_id):\n"
        "    ticket = db.get(Ticket, ticket_id)\n"
        "    ticket.status = 'closed'\n",
    )
    assert _counts(tmp_path) == {}


def test_detector_reports_an_unscanned_python_root(tmp_path: Path) -> None:
    """An entry-point family living outside every scanned root is reported."""

    _synthetic_tree(
        tmp_path,
        "workers/nightly.py",
        "from app.models.party import Party\n"
        "def run(db):\n"
        "    db.add(Party(party_type='person', display_name='x'))\n",
    )
    assert unscanned_python_roots(project_root=tmp_path) == ("workers/",)


def test_an_excluded_root_is_not_reported(tmp_path: Path) -> None:
    """The acceptance half of the unscanned-root check."""

    _synthetic_tree(tmp_path, "tests/test_thing.py", "def test_x():\n    pass\n")
    assert unscanned_python_roots(project_root=tmp_path) == ()


def test_stale_baseline_after_writer_removal_is_a_failure(tmp_path: Path) -> None:
    """Removing a writer without lowering its line must fail the ratchet."""

    baseline = _read_baseline(BASELINE)
    survivor = "service|app/services/party.py"
    assert survivor in baseline, "fixture assumption: party.py is a baselined writer"

    pretend_current = {name: count for name, count in baseline.items()}
    del pretend_current[survivor]

    retired = sorted(set(baseline) - set(pretend_current))
    assert retired == [survivor], (
        "the staleness comparison this guard performs must notice a writer "
        "that vanished from the census while its baseline line remained"
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/api/customers.py", EntryPointFamily.API_ROUTE),
        ("app/api/crm_webhooks.py", EntryPointFamily.WEBHOOK_HANDLER),
        ("app/api/payment_callback.py", EntryPointFamily.WEBHOOK_HANDLER),
        ("app/web/admin/customers.py", EntryPointFamily.WEB_ROUTE),
        ("app/services/web_customer_actions.py", EntryPointFamily.WEB_PRESENTER),
        ("app/services/party.py", EntryPointFamily.SERVICE),
        ("app/services/events/handlers/thing.py", EntryPointFamily.EVENT_HANDLER),
        ("app/tasks/nin_tasks.py", EntryPointFamily.TASK_WORKER),
        ("app/celery_scheduler.py", EntryPointFamily.SCHEDULED_JOB),
        ("app/websocket/hub.py", EntryPointFamily.WEBSOCKET),
        ("app/imports/loader.py", EntryPointFamily.IMPORTER),
        ("app/poller/snmp.py", EntryPointFamily.POLLER),
        ("app/syslog/reader.py", EntryPointFamily.POLLER),
        ("app/db.py", EntryPointFamily.APP_MODULE),
        ("scripts/one_off/thing.py", EntryPointFamily.CLI_SCRIPT),
        ("alembic/versions/999_thing.py", EntryPointFamily.MIGRATION),
        ("stray.py", EntryPointFamily.REPOSITORY_ROOT),
    ],
)
def test_every_declared_family_is_reachable(
    path: str, expected: EntryPointFamily
) -> None:
    """Each declared family must be produced by some real path shape.

    A family nobody can reach is decoration: it makes the enumeration look
    complete while covering nothing.
    """

    assert family_for(path) is expected


def test_the_ownership_map_lists_every_production_writer() -> None:
    """The prose map and the classified inventory must name the same files.

    The document is what a reviewer reads before a cutover pull request. A
    writer that exists in the inventory and not in the document is a writer
    nobody will plan to displace.
    """

    document = (
        Path(__file__).resolve().parents[2] / "docs" / "ISP_COHORT1_SOURCE_OWNERSHIP.md"
    ).read_text(encoding="utf-8")

    missing = sorted(
        path for path in surfaces.production_writer_paths() if path not in document
    )
    assert not missing, (
        "docs/ISP_COHORT1_SOURCE_OWNERSHIP.md does not mention these "
        "production writers of cohort-isp-01 state:\n  " + "\n  ".join(missing)
    )


def test_every_writer_is_also_a_reader() -> None:
    """Coherence between the two censuses.

    A file cannot write a cohort table without naming it, so the reference
    census must contain every writer. If it does not, the reference scan has a
    gap and its per-family totals understate the reach a cutover has to
    consider.
    """

    writers = {site.path for site in cohort_write_sites()}
    referenced = {path for _, path in cohort_reference_sites()}
    missing = sorted(writers - referenced)
    assert not missing, (
        "these files write cohort state and the reference census does not see "
        "them naming it:\n  " + "\n  ".join(missing)
    )


def test_the_reader_reach_is_wider_than_the_writer_surface() -> None:
    """The reference census must actually be measuring something wider.

    Not a numeric assertion — the count moves with any unrelated file that
    mentions `Subscriber`. What is asserted is the shape: many more readers
    than writers, across more entry-point families. A reference census that
    collapsed to the writer set would be reporting the wrong thing while
    looking healthy.
    """

    reference_families = set(reference_counts_by_family())
    writer_families = {site.family for site in cohort_write_sites()}
    assert writer_families <= reference_families
    assert len(reference_families) > len(writer_families), (
        "the reference census sees no entry-point family the writer census "
        "does not; readers reach further than writers and the scan should "
        "show it"
    )
    assert len(cohort_reference_sites()) > 5 * len(cohort_write_sites())
