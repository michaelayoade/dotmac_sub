"""`main` is the only trunk, in the workflows AND in the prose that governs them.

`dev` was deleted on 2026-08-28 and every workflow trigger was moved to `main`.
The remaining exposure is not the configuration -- it is the instruction: a
normative document that still routes work through `dev` sends the next agent or
operator down a path that cannot exist, and prose has no compiler. That is not
hypothetical. `AGENTS.md` is the file this repository tells every contributor to
read first, and a stale `feature -> dev -> main` sentence there outranks a
correct workflow file in practice, because the human and the agent both read the
sentence and neither reads the YAML.

So this module refuses the revival from BOTH sides and, critically, compares
them rather than checking each in isolation:

* every workflow trigger's branch filters are parsed out of the YAML and must
  name only the declared trunk and its short-lived batch branches;
* every normative document may mention `dev` only in a sentence that RETIRES it;
* the trunk that the prose declares must be the trunk the triggers actually use.

The last one is the point. Two independent literal-string checks can both pass
while describing different worlds; deriving the branch set from the parsed
configuration and comparing it against the branch the documents declare is what
makes a disagreement fail.

This complements, rather than repeats,
`test_staging_promotion_workflow.py::test_main_is_the_only_release_trunk_in_the_whole_chain`,
which greps a HAND-LISTED release chain for the spelling `dev`. That guard
cannot see a workflow added tomorrow, says nothing about prose, and compares
nothing. This one is glob-swept, parsed, prose-inclusive, comparative, and
carries a sensitivity proof.

Scope, stated rather than implied (ADR-0018): NORMATIVE_DOCUMENTS is the set of
tracked documents that INSTRUCT. Dated evidence -- triage reports, adoption
ledgers, handovers, and design documents recording the revision they were
measured against -- legitimately names `origin/dev` as a historical coordinate
and is deliberately UNMONITORED here rather than exempted, because "the commit I
measured" is a fact about the past that no future reader can act on.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

# The declared trunk, and the short-lived batch prefixes that run the same
# required gates. A batch branch assembles several pull requests so the batch
# meets CI before it reaches `main`; nothing is ever promoted from one branch to
# another, which is what makes these assembly branches rather than a trunk.
TRUNK = "main"
BATCH_BRANCHES = frozenset({"integration/**", "consolidate/**"})
PERMITTED_TRIGGER_BRANCHES = frozenset({TRUNK}) | BATCH_BRANCHES

# Documents that tell a human or an agent what to do. A revived `dev` hop here
# is an instruction, which is why these are scanned and dated records are not.
NORMATIVE_DOCUMENTS = (
    "AGENTS.md",
    "docs/CI_CD_PIPELINE.md",
    "docs/runbooks/STAGING_PROMOTION.md",
    "docs/runbooks/PRODUCTION_DEPLOYMENT.md",
    "docs/designs/RELEASE_ARTIFACT_PROMOTION.md",
)

# A sentence may name `dev` only while marking it as gone -- retired, removed,
# past, or forbidden to revive. This is the vocabulary the repository actually
# uses to do that; it is deliberately about the STATUS of the branch, never
# about the topic of the sentence, so an instruction that happens to discuss
# releases is not excused. A revival carries none of these, which is what
# `test_the_single_trunk_guard_bites` demonstrates.
RETIREMENT_MARKERS = (
    "no long-lived",
    "there is no",
    "no `dev` branch",
    "retir",  # retired / retiring / retirement
    "reintroduc",  # (not) reintroduced / if ... is ever reintroduced
    "removed",
    "deleted",
    "former",
    "no longer",
    "than it did",
    "used to",
    "old rule",
)

# A branch named `dev`. NOT `/dev/null` (a device path), NOT `development` (a
# word), and NOT a dotted attribute such as Poetry's `dependency-groups.dev`
# (a dependency group) -- hence `.` in the lookbehind.
_DEV_TOKEN = re.compile(r"(?<![\w/.-])dev(?![\w-])")
_TRIGGERS_WITH_BRANCHES = (
    "push",
    "pull_request",
    "pull_request_target",
    "workflow_run",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _trigger_branches(workflow_text: str) -> set[str]:
    """Every branch a workflow's triggers name, from the PARSED configuration.

    Reading the YAML rather than grepping it is what lets this compare against
    the declared trunk: a grep can only ask whether a spelling is present, not
    which set of branches the file actually selects.
    """

    document = yaml.safe_load(workflow_text)
    if not isinstance(document, dict):
        return set()
    # PyYAML resolves the bare key `on` to the boolean True.
    triggers = document.get("on", document.get(True))
    if not isinstance(triggers, dict):
        return set()

    branches: set[str] = set()
    for name in _TRIGGERS_WITH_BRANCHES:
        trigger = triggers.get(name)
        if not isinstance(trigger, dict):
            continue
        # `branches` only. `branches-ignore` names what a workflow REFUSES to
        # run on, so a hypothetical `branches-ignore: [dev]` is the trunk rule
        # being obeyed, not broken -- folding it in here would fail a correct
        # configuration.
        value = trigger.get("branches")
        if isinstance(value, str):
            branches.add(value)
        elif isinstance(value, list):
            branches.update(str(entry) for entry in value)
    return branches


def _sentences(document_text: str) -> list[str]:
    """Split on sentence ends only.

    A colon does NOT end a sentence: it introduces the clause that explains the
    one before it, and in this repository's prose the retirement is regularly
    stated before the colon and the retired mechanism described after it.
    Splitting there would strip the explanation from its own subject and report
    a retirement paragraph as an instruction.
    """

    collapsed = " ".join(document_text.split())
    return [
        part.strip() for part in re.split(r"(?<=[.;])\s+", collapsed) if part.strip()
    ]


def _dev_first_sentences(document_text: str) -> list[str]:
    """Sentences that name a `dev` branch WITHOUT retiring it."""

    offending = []
    for sentence in _sentences(document_text):
        candidate = sentence.replace("/dev/null", "").replace("development", "")
        if not _DEV_TOKEN.search(candidate):
            continue
        lowered = sentence.lower()
        if any(marker in lowered for marker in RETIREMENT_MARKERS):
            continue
        offending.append(sentence)
    return offending


def _workflow_paths() -> list[Path]:
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert paths, "no workflows found; the trunk guard would pass vacuously"
    return paths


def test_no_workflow_trigger_names_a_second_trunk() -> None:
    """Swept by glob, not by a hand-maintained list.

    A hand-listed set of release workflows cannot notice the file that is added
    tomorrow, and the revival this guards against is exactly a new workflow that
    quietly triggers on a second branch.
    """

    for path in _workflow_paths():
        branches = _trigger_branches(path.read_text(encoding="utf-8"))
        unexpected = branches - PERMITTED_TRIGGER_BRANCHES
        assert not unexpected, (
            f"{path.relative_to(ROOT)} triggers on {sorted(unexpected)}, which is "
            f"not the declared trunk {TRUNK} or a batch branch "
            f"{sorted(BATCH_BRANCHES)}"
        )


def test_the_trunk_the_triggers_use_is_the_trunk_the_documents_declare() -> None:
    """The comparison, not two isolated checks.

    Both halves can be internally consistent and still describe different
    worlds. This derives the trunk from the parsed triggers and requires the
    normative documents to declare that same branch.
    """

    triggered = set()
    for path in _workflow_paths():
        triggered |= _trigger_branches(path.read_text(encoding="utf-8"))

    assert TRUNK in triggered, (
        "no workflow triggers on the declared trunk; either the trunk moved or "
        "the required gates no longer run on it"
    )
    assert triggered <= PERMITTED_TRIGGER_BRANCHES

    guidance = " ".join(_read("AGENTS.md").split())
    assert f"`{TRUNK}` is the single release trunk" in guidance
    assert f"`{TRUNK}` is the machine-owned integration and release branch" in guidance
    assert "repository default branch and the ONLY protected branch" in guidance
    assert "`feature -> dev -> main` is retired" in guidance


def test_normative_documents_never_revive_the_dev_first_hop() -> None:
    """A `dev` mention is allowed only in the sentence that retires it."""

    for document in NORMATIVE_DOCUMENTS:
        offending = _dev_first_sentences(_read(document))
        assert not offending, (
            f"{document} names a `dev` branch outside a retirement statement, "
            f"which reads as an instruction: {offending}"
        )


def test_the_single_trunk_guard_bites() -> None:
    """Sensitivity proof -- both detectors are shown failing on a revival.

    A guard that has never been observed to fail is not evidence. Each detector
    is run against a mutated copy carrying exactly the defect it exists to
    catch, and against the real text, so a passing suite means the detector
    discriminates rather than merely tolerates.
    """

    # 1. A workflow trigger that adds a second trunk.
    revived_workflow = """
name: CI
on:
  push:
    branches: [main, dev]
  pull_request:
    branches: [main]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: 'true'
"""
    assert _trigger_branches(revived_workflow) - PERMITTED_TRIGGER_BRANCHES == {"dev"}

    # ...and the real configuration is clean, so the assertion above is not
    # passing because the parser returns everything it sees.
    for path in _workflow_paths():
        assert not _trigger_branches(path.read_text(encoding="utf-8")) - (
            PERMITTED_TRIGGER_BRANCHES
        )

    # 2. Prose that reinstates the hop.
    revived_prose = (
        "Work on a feature branch. Merge the feature branch into `dev`. "
        "Promote `dev` to `main` once staging accepts it."
    )
    assert len(_dev_first_sentences(revived_prose)) == 2

    # 3. The retirement sentences the repository actually ships must NOT trip
    #    it, or the detector would only be usable by deleting the explanation.
    retirement_prose = (
        "There is no long-lived `dev` branch and no branch-to-branch promotion "
        "hop. `feature -> dev -> main` is retired and must not be reintroduced."
    )
    assert _dev_first_sentences(retirement_prose) == []

    # 4. `/dev/null`, `development`, and a dotted `...groups.dev` attribute are
    #    not branch names.
    assert (
        _dev_first_sentences(
            "Redirect output to /dev/null in development; tools belong to "
            "`dependency-groups.dev` only."
        )
        == []
    )


@pytest.mark.parametrize("document", NORMATIVE_DOCUMENTS)
def test_every_normative_document_exists(document: str) -> None:
    """The scan set must not silently shrink to nothing."""

    assert (ROOT / document).is_file(), (
        f"{document} is listed as normative but is missing; either restore it "
        "or remove it from NORMATIVE_DOCUMENTS deliberately"
    )
