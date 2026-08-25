"""Ratchet Sub's own direct external-connector surface, in Sub's own CI.

This is the gate that actually runs today. `.github/workflows/
external-connector-ratchet.yml` stages the Governance engine's version of the
same rules, but dotmac_governance ADR 0010 is `Proposed`, there is no accepted
revision to pin, and that workflow fails closed against the placeholder on
purpose. Meanwhile this module runs in the existing `Architecture Tests` job on
every pull request, against this tree, with thresholds read from the one staged
profile object — so the baseline is enforced rather than merely written down.

The fleet sweep in `dotmac_starter_mt/scripts/external_connector_sweep.py`
cannot substitute for it: it measures sibling repositories from one checkout and
reports UNMEASURED for `dotmac_sub` in starter CI, where no `dotmac_sub`
checkout exists. A repository unmeasured by the only gate is ungated, so a
central sweep must never be the only gate.

Every detector below carries a sensitivity proof, per starter ADR 0018: it is
shown to FIRE on a minimal positive and to STAY SILENT on the nearest negative,
and the ratchet itself is shown to fail in both directions. A check that only
ever passes is not evidence.
"""

from __future__ import annotations

import ast
import json
import subprocess
from functools import cache
from pathlib import Path, PurePosixPath

import pytest
import yaml

from tests.architecture.external_connector_surface import (
    AMBIGUOUS_CHECKPOINT_CLASS_HINTS,
    CATEGORIES,
    FEED_CHECKPOINT_CLASS_HINTS,
    LIVE_PROFILE,
    PENDING_PIN,
    STAGED_PROFILE,
    Measurement,
    StagedProfileError,
    classify,
    is_checkpoint_class_name,
    measure,
    ratchet,
    runtime_files,
    staged_baselines,
    staged_profile,
    staged_runtime_roots,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "external-connector-ratchet.yml"
PIN_GUARD_STEP = "Refuse an unapproved Governance revision"

#: What the Governance profile loader accepts as a pinned revision
#: (`standards_control/profile.py`, `GIT_REVISION`). Mirrored rather than
#: imported because dotmac_governance is not a dependency of this repository —
#: which is precisely why the pin has to be checked here too.
GIT_REVISION_LENGTH = 40
GIT_REVISION_ALPHABET = frozenset("0123456789abcdef")

#: A real-looking commit used only to drive the pin guard's accepting branch.
#: It pins nothing: it exists so the guard can be shown to fail in BOTH
#: directions rather than merely to fail.
FAKE_ACCEPTED_REVISION = "0" * GIT_REVISION_LENGTH


def _is_git_revision(value: str) -> bool:
    return len(value) == GIT_REVISION_LENGTH and set(value) <= GIT_REVISION_ALPHABET


@cache
def _measured() -> Measurement:
    return measure()


@cache
def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _workflow_pin() -> str:
    return str(_workflow()["env"]["GOVERNANCE_REF"])


def _workflow_triggers() -> set[str]:
    """The `on:` keys.

    YAML 1.1 reads a bare `on` as the boolean true, so PyYAML hands back `True`
    as the key. Reading both spellings keeps this from depending on whether the
    workflow author quoted it.
    """
    document = _workflow()
    section = document.get("on", document.get(True))
    assert section is not None, f"{WORKFLOW.name} declares no triggers"
    return set(section)


def _pin_guard_script() -> str:
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == PIN_GUARD_STEP:
                return str(step["run"])
    raise AssertionError(
        f"{WORKFLOW.name} has no {PIN_GUARD_STEP!r} step; the pin barrier is "
        "the first thing the staged job must do"
    )


def _run_pin_guard(revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", "-c", _pin_guard_script()],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "GOVERNANCE_REF": revision},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


# --- The live measurement --------------------------------------------------


def test_the_declared_runtime_roots_exist() -> None:
    for root in staged_runtime_roots():
        assert (ROOT / root).is_dir(), (
            f"declared runtime root {root} is missing; the connector surface "
            "would measure zero for code that still exists"
        )


def test_no_runtime_file_is_unmeasurable() -> None:
    """A file the detector cannot parse must fail closed, never score zero."""
    assert not _measured().unmeasurable, (
        "runtime files could not be measured for connector surfaces:\n  "
        + "\n  ".join(_measured().unmeasurable)
    )


def test_an_unparseable_source_fails_closed_rather_than_measuring_zero() -> None:
    with pytest.raises(SyntaxError):
        classify("def broken(:\n    pass\n")


def test_the_measured_connector_surface_matches_the_declared_baseline() -> None:
    failures = ratchet(_measured().counts, staged_baselines())

    assert not failures, "\n".join(
        [*failures, "", "measured files behind each category:", *_evidence()]
    )


def _evidence() -> list[str]:
    lines: list[str] = []
    for category in CATEGORIES:
        paths = _measured().files_by_category[category]
        lines.append(f"  {category} ({len(paths)}):")
        lines.extend(f"    {path}" for path in paths)
    return lines


def test_the_baseline_declares_every_closed_category() -> None:
    """The six categories are closed, so none can be dropped to hide a count."""
    assert sorted(staged_baselines()) == sorted(CATEGORIES)


# --- Sensitivity of the ratchet itself -------------------------------------


def test_the_ratchet_fails_in_both_directions() -> None:
    baselines = dict.fromkeys(CATEGORIES, 3)

    assert ratchet(dict(baselines), baselines) == []

    for category in CATEGORIES:
        risen = {**baselines, category: 4}
        failures = ratchet(risen, baselines)
        assert len(failures) == 1, failures
        assert failures[0].startswith(f"{category}: 4 measured")
        assert "exceed the declared baseline 3" in failures[0]

        fallen = {**baselines, category: 2}
        failures = ratchet(fallen, baselines)
        assert len(failures) == 1, failures
        assert failures[0].startswith(f"{category}: 2 measured")
        assert "below the declared baseline 3" in failures[0]


def test_raising_a_baseline_to_absorb_a_new_surface_is_refused() -> None:
    """The move this ratchet exists to stop: bump the number, keep the code."""
    observed = dict(_measured().counts)
    inflated = {**staged_baselines(), "sync_checkpoint": 9}

    failures = ratchet(observed, inflated)
    stale = [
        failure
        for failure in failures
        if failure.startswith("sync_checkpoint:") and "below" in failure
    ]

    assert stale, (
        "raising sync_checkpoint from 8 to 9 without adding code must fail as "
        f"a stale baseline, but the ratchet said: {failures}"
    )


def test_dropping_a_category_from_the_profile_is_refused() -> None:
    incomplete = {
        key: value for key, value in staged_baselines().items() if key != "http_client"
    }

    failures = ratchet(_measured().counts, incomplete)

    assert any("http_client" in failure for failure in failures), failures


# --- Sensitivity of each of the six detectors ------------------------------

#: category -> (a minimal source that IS one, the nearest source that is NOT).
#: Table-driven and asserted complete over `CATEGORIES` below, so a seventh
#: category cannot be added without a proof that its detector bites.
DETECTOR_PROOFS: dict[str, tuple[str, str]] = {
    "http_client": (
        "import httpx\n\n\ndef fetch():\n    return httpx.get('https://x.invalid')\n",
        # Imported for a type annotation only: no request is ever issued.
        "import httpx\n\n\ndef build(client: httpx.Client) -> httpx.Client:\n"
        "    return client\n",
    ),
    "webhook_surface": (
        "@router.post('/webhooks/paystack')\nasync def receive(payload: dict):\n"
        "    return payload\n",
        # A product API route, not a provider callback.
        "@router.post('/customers')\nasync def create(payload: dict):\n"
        "    return payload\n",
    ),
    "provider_credential": (
        "class Settings:\n    paystack_secret_key: str = ''\n",
        # Sub's own key, named for no provider — deliberately not counted.
        "class Settings:\n    api_key: str = ''\n",
    ),
    "connector_task": (
        "from celery import shared_task\n\n\n@shared_task\n"
        "def sync_provider_invoices() -> None:\n    return None\n",
        # A scheduled task whose subject is local work, not a feed.
        "from celery import shared_task\n\n\n@shared_task\n"
        "def rebuild_local_index() -> None:\n    return None\n",
    ),
    "sync_checkpoint": (
        "class ProviderFeedCheckpoint:\n    last_event_id: str\n",
        # A pagination cursor: the ordinary meaning of the word.
        "class PaginationCursor:\n    offset: int\n",
    ),
    "delivery_retry": (
        "import httpx\n\nMAX_RETRIES = 3\n\n\ndef deliver():\n"
        "    return httpx.post('https://x.invalid')\n",
        # Retry machinery around a local write is not delivery machinery.
        "MAX_RETRIES = 3\n\n\ndef rebuild() -> None:\n    return None\n",
    ),
}


def test_every_category_carries_a_sensitivity_proof() -> None:
    assert sorted(DETECTOR_PROOFS) == sorted(CATEGORIES)


@pytest.mark.parametrize("category", sorted(DETECTOR_PROOFS))
def test_each_detector_fires_on_a_positive_and_is_silent_on_the_negative(
    category: str,
) -> None:
    positive, negative = DETECTOR_PROOFS[category]

    assert category in classify(positive), (
        f"the {category} detector did not fire on a minimal positive; a "
        "detector that cannot see the thing it guards is decoration"
    )
    assert category not in classify(negative), (
        f"the {category} detector fired on its nearest negative; an overcount "
        "gets the ratchet switched off"
    )


# --- The corrected cursor classification -----------------------------------


def _classes_named(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def test_the_inbox_round_robin_cursor_is_not_a_feed_checkpoint() -> None:
    """The miscount this correction was found by, asserted against the real class.

    `InboxTeamRoundRobinCursor` is durable per-team ROTATION state for Inbox
    assignment: foreign-keyed to local `service_teams`, carrying
    `last_assigned_person_id` and `rotation_count`, with no watermark, no feed
    and zero external references. It matched the fleet sweep only because
    "cursor" was a substring hint.
    """
    relative = "app/models/team_inbox.py"

    assert "InboxTeamRoundRobinCursor" in _classes_named(relative), (
        f"{relative} no longer defines InboxTeamRoundRobinCursor; this proof "
        "must be repointed at whatever replaced it rather than deleted"
    )
    assert not is_checkpoint_class_name("InboxTeamRoundRobinCursor")

    counted_in = [
        category
        for category in CATEGORIES
        if relative in _measured().files_by_category[category]
    ]
    assert not counted_in, (
        f"{relative} is counted as {counted_in}; rotation state is not an "
        "external-connector surface"
    )


def test_sub_real_feed_checkpoints_are_still_counted() -> None:
    """The three real ones, by class and by measured file."""
    expected = {
        "app/models/erp_domain_sync.py": "ErpDomainSyncCursor",
        "app/models/integration_platform.py": "IntegrationCheckpoint",
        "app/models/quote_mirror.py": "QuoteSyncState",
    }
    counted = set(_measured().files_by_category["sync_checkpoint"])

    for relative, class_name in expected.items():
        assert class_name in _classes_named(relative), (
            f"{relative} no longer defines {class_name}; lower the "
            "sync_checkpoint baseline in the change that retired it"
        )
        assert is_checkpoint_class_name(class_name)
        assert relative in counted


def test_narrowing_the_bare_cursor_rule_costs_no_recall_in_this_repository() -> None:
    """The whole correction, measured against Sub's real tree.

    Re-measures `sync_checkpoint` with the OLD, broad rule — bare `*Cursor`
    counting on its own — and asserts the ONLY file the broad rule adds is the
    round-robin rotation pointer. Everything the broad rule found that is a
    real feed position is still found, because it is either named for its feed
    or caught by the unchanged COLUMN net.
    """
    broad_hints = FEED_CHECKPOINT_CLASS_HINTS + AMBIGUOUS_CHECKPOINT_CLASS_HINTS
    broadly_counted: set[str] = set()
    for relative in runtime_files(ROOT, staged_runtime_roots()):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                hint in node.name.lower() for hint in broad_hints
            ):
                broadly_counted.add(relative.as_posix())

    narrowly_counted = set(_measured().files_by_category["sync_checkpoint"])
    lost = broadly_counted - narrowly_counted

    assert lost == {"app/models/team_inbox.py"}, (
        "narrowing the bare `*Cursor` rule changed the measured set by more "
        f"than the known rotation pointer: {sorted(lost)}"
    )


#: Class names that must count as a feed position, and names that must not.
#: The positives are the mutation proof demanded of the narrowed rule: a real
#: upstream cursor, a webhook cursor and a provider-named cursor all still
#: qualify without any generic word.
COUNTS_AS_FEED_POSITION = (
    "UpstreamInvoiceCursor",
    "WebhookDeliveryCursor",
    "PaystackPayoutCursor",
    "ErpnextItemCursor",
    "RemoteCatalogCursor",
    "IngestCursor",
    "SyncCursor",
    "ProviderCheckpoint",
    "BillingSyncState",
)
DOES_NOT_COUNT_AS_FEED_POSITION = (
    "InboxTeamRoundRobinCursor",
    "PaginationCursor",
    "DatabaseCursor",
    "MetadataCursor",
    "RoundRobinCursor",
    "ResultCursor",
)


@pytest.mark.parametrize("class_name", COUNTS_AS_FEED_POSITION)
def test_a_cursor_that_names_its_feed_is_detected(class_name: str) -> None:
    assert is_checkpoint_class_name(class_name)
    assert "sync_checkpoint" in classify(f"class {class_name}:\n    position: str\n")


@pytest.mark.parametrize("class_name", DOES_NOT_COUNT_AS_FEED_POSITION)
def test_a_cursor_that_names_no_feed_is_not_detected(class_name: str) -> None:
    assert not is_checkpoint_class_name(class_name)
    assert "sync_checkpoint" not in classify(
        f"class {class_name}:\n    position: str\n"
    )


@pytest.mark.parametrize(
    "column", ["last_synced_at", "sync_cursor", "last_cursor", "watermark"]
)
def test_a_watermark_bearing_model_is_detected_whatever_it_is_called(
    column: str,
) -> None:
    """Why narrowing the NAME rule costs no recall: the COLUMN net is unchanged.

    The class here is named for rotation, exactly like the false positive, and
    still counts — because it actually stores a position in a feed.
    """
    source = f"class RoundRobinRotation:\n    {column}: str\n"

    assert "sync_checkpoint" in classify(source)


# --- One set of numbers ----------------------------------------------------


def test_exactly_one_profile_declares_the_connector_surface() -> None:
    surface = staged_profile()["external_connector_surface"]

    assert set(surface) == {"runtime_roots", "baselines", "exclusions"}


def test_two_declaring_profiles_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two profiles would be two thresholds, which is the drift to prevent."""
    body = json.dumps({"external_connector_surface": {}})
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(body, encoding="utf-8")
    second.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        "tests.architecture.external_connector_surface.STAGED_PROFILE", first
    )
    monkeypatch.setattr(
        "tests.architecture.external_connector_surface.LIVE_PROFILE", second
    )

    with pytest.raises(StagedProfileError, match="both the staged and live"):
        staged_profile()


def test_no_declaring_profile_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ratchet whose thresholds vanished must fail, not silently pass."""
    empty = tmp_path / "a.json"
    empty.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "tests.architecture.external_connector_surface.STAGED_PROFILE", empty
    )
    monkeypatch.setattr(
        "tests.architecture.external_connector_surface.LIVE_PROFILE", empty
    )

    with pytest.raises(StagedProfileError, match="no profile declares"):
        staged_profile()


def test_the_staged_profile_declares_no_exclusion_this_gate_cannot_evaluate() -> None:
    """Exclusions are a Governance-engine feature; this local mirror has none.

    ADR 0010 makes an exclusion carry a typed, machine-checkable `premise` that
    the engine independently verifies, and a false premise suppresses nothing.
    This module ports the detectors but not the premise evaluator, so an
    exclusion added here would suppress a count centrally while being invisible
    locally. The first exclusion may therefore only land in the change that
    pins the accepted Governance revision and makes the engine the evaluator.
    """
    exclusions = staged_profile()["external_connector_surface"]["exclusions"]

    assert exclusions == [], (
        "the staged profile declares exclusions that Sub's local ratchet "
        f"cannot evaluate: {exclusions}"
    )


def test_the_runtime_roots_are_application_runtime_not_a_test_tree() -> None:
    for root in staged_runtime_roots():
        assert PurePosixPath(root).name not in {"tests", "test"}


# --- The pin barrier -------------------------------------------------------


def test_the_staged_profile_carries_the_placeholder_pin() -> None:
    profile = staged_profile()

    assert profile["schema_version"] == 6
    revision = profile["governance_model"]["revision"]
    assert revision == PENDING_PIN
    assert not _is_git_revision(revision), (
        "the placeholder must remain something the Governance profile loader "
        "refuses; a loadable value would let the staged profile be evaluated "
        "as if ADR 0010 had been accepted"
    )


def test_the_staged_workflow_carries_the_same_placeholder_pin() -> None:
    assert _workflow_pin() == PENDING_PIN
    assert not _is_git_revision(_workflow_pin())


def test_the_pin_guard_refuses_the_placeholder() -> None:
    result = _run_pin_guard(PENDING_PIN)

    assert result.returncode != 0, (
        "the staged connector job reported success against "
        f"{PENDING_PIN!r}; it must fail closed until an accepted Governance "
        f"commit is pinned.\nstdout:\n{result.stdout}"
    )
    assert PENDING_PIN in result.stdout
    assert "Proposed" in result.stdout


def test_the_pin_guard_accepts_a_real_commit_shaped_revision() -> None:
    """The other direction: the guard is a pin check, not an unconditional fail."""
    result = _run_pin_guard(FAKE_ACCEPTED_REVISION)

    assert result.returncode == 0, (
        f"the pin guard rejected a well-formed revision.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert FAKE_ACCEPTED_REVISION in result.stdout


@pytest.mark.parametrize(
    "revision",
    [
        "main",
        "dev",
        "v1.0.0",
        "PENDING",
        "FBD47B8965002943BD5799992F4B29B04E361582",
        "fbd47b8965002943bd5799992f4b29b04e36158",
        "",
    ],
)
def test_the_pin_guard_refuses_every_non_commit_revision(revision: str) -> None:
    """A branch, a tag, a short SHA or an upper-case SHA is not a pinned commit."""
    assert _run_pin_guard(revision).returncode != 0


def test_the_staged_workflow_triggers_stay_coupled_to_the_pin() -> None:
    """Two-directional coupling, so the two halves cannot drift apart.

    While the pin is a placeholder the job must be `workflow_dispatch`-only: a
    required check that can only be red deadlocks the merge queue. Once a real
    commit is pinned the job must gain `pull_request` and `merge_group`, or the
    connector surface has a Governance profile nobody evaluates.
    """
    triggers = _workflow_triggers()

    if _is_git_revision(_workflow_pin()):
        assert {"pull_request", "merge_group"} <= triggers, (
            "an accepted Governance revision is pinned, so this job must run "
            "on pull requests and in the merge queue; also make the check "
            "required on dev"
        )
    else:
        assert triggers == {"workflow_dispatch"}, (
            "the pin is still the placeholder, so this job can only ever be "
            f"red; it must not be wired to {sorted(triggers)} until the "
            "accepted commit is pinned"
        )


def test_the_live_profile_is_not_migrated_before_the_pin_lands() -> None:
    """The required check must keep working while ADR 0010 is unaccepted.

    The live profile is read by the ACCEPTED engine, which is a schema-5 parser
    and refuses `schema_version: 6` outright. Migrating it today would take the
    required `Dotmac engineering standards` check down repository-wide.
    """
    live = json.loads(LIVE_PROFILE.read_text(encoding="utf-8"))

    if _is_git_revision(_workflow_pin()):
        return
    assert live["schema_version"] == 5
    assert "external_connector_surface" not in live
    assert _is_git_revision(live["governance_model"]["revision"]), (
        "the LIVE profile must keep its real accepted pin; only the staged "
        "schema-6 profile carries the placeholder"
    )


def test_the_staged_profile_is_the_one_that_holds_the_placeholder() -> None:
    staged = json.loads(STAGED_PROFILE.read_text(encoding="utf-8"))

    assert staged["governance_model"]["revision"] == PENDING_PIN
    assert "external_connector_surface" in staged
