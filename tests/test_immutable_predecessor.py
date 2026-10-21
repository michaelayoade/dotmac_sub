"""Offline Git topology tests; synthetic commits are fixtures, not release evidence."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from scripts.ci.immutable_predecessor import (
    ERP_ACCOUNTING_FACT_PREDECESSOR,
    ImmutablePredecessor,
    PredecessorError,
    PredecessorRefusal,
    acquire_predecessor,
    verify_predecessor,
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


@dataclass(frozen=True)
class SquashRepository:
    checkout: Path
    predecessor: ImmutablePredecessor
    head: str
    refs: str


@pytest.fixture
def squash_repository(tmp_path: Path) -> SquashRepository:
    """Only a local bare origin; the old commit lives on a separate source ref."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "CI fixture")
    _git(source, "config", "user.email", "ci@example.test")
    (source / "seed").write_text("base\n", encoding="utf-8")
    _git(source, "add", "seed")
    _git(source, "commit", "-q", "-m", "fixture base")
    _git(source, "checkout", "-q", "-b", "old-source")
    (source / "old-model").write_text("original enum\n", encoding="utf-8")
    _git(source, "add", "old-model")
    _git(source, "commit", "-q", "-m", "fixture old source")
    predecessor = ImmutablePredecessor(
        commit=_git(source, "rev-parse", "HEAD"),
        tree=_git(source, "rev-parse", "HEAD^{tree}"),
    )
    _git(source, "checkout", "-q", "-b", "candidate", "main")
    _git(source, "merge", "--squash", "old-source")
    (source / "candidate-migration").write_text("add enum label\n", encoding="utf-8")
    _git(source, "add", "candidate-migration")
    _git(source, "commit", "-q", "-m", "fixture squash candidate")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(origin))
    checkout = tmp_path / "checkout"
    # --no-local avoids copying unrelated old-source objects from the bare repo.
    _git(
        tmp_path,
        "clone",
        "-q",
        "--no-local",
        "--single-branch",
        "--branch",
        "candidate",
        str(origin),
        str(checkout),
    )
    return SquashRepository(
        checkout=checkout,
        predecessor=predecessor,
        head=_git(checkout, "rev-parse", "HEAD"),
        refs=_git(checkout, "show-ref"),
    )


def test_exact_missing_predecessor_is_acquired_despite_squash_topology(
    squash_repository: SquashRepository,
) -> None:
    fixture = squash_repository
    with pytest.raises(PredecessorError) as missing:
        verify_predecessor(repository=fixture.checkout, source=fixture.predecessor)
    assert missing.value.code is PredecessorRefusal.MISSING_OBJECT

    verified = acquire_predecessor(
        repository=fixture.checkout, source=fixture.predecessor
    )
    assert verified.source == fixture.predecessor
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", verified.source.commit, "HEAD"],
        cwd=fixture.checkout,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert ancestry.returncode == 1
    assert _git(fixture.checkout, "rev-parse", "HEAD") == fixture.head
    assert _git(fixture.checkout, "show-ref") == fixture.refs
    assert not (fixture.checkout / ".git" / "FETCH_HEAD").exists()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", verified.source.commit, "old-model"],
        cwd=fixture.checkout,
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert b"original enum" in archive.stdout
    assert b"candidate-migration" not in archive.stdout
    # Once acquired, verification succeeds locally even with origin removed.
    _git(fixture.checkout, "remote", "remove", "origin")
    assert (
        acquire_predecessor(repository=fixture.checkout, source=fixture.predecessor)
        == verified
    )


def test_unavailable_exact_sha_fails_without_branch_fallback(
    squash_repository: SquashRepository,
) -> None:
    fixture = squash_repository
    unknown = replace(fixture.predecessor, commit="f" * 40)
    with pytest.raises(PredecessorError) as refusal:
        acquire_predecessor(repository=fixture.checkout, source=unknown)
    assert refusal.value.code is PredecessorRefusal.FETCH_FAILED
    assert _git(fixture.checkout, "rev-parse", "HEAD") == fixture.head
    assert _git(fixture.checkout, "show-ref") == fixture.refs


def test_wrong_pinned_tree_refuses_after_exact_acquisition(
    squash_repository: SquashRepository,
) -> None:
    fixture = squash_repository
    wrong = replace(
        fixture.predecessor, tree=_git(fixture.checkout, "rev-parse", "HEAD^{tree}")
    )
    with pytest.raises(PredecessorError) as refusal:
        acquire_predecessor(repository=fixture.checkout, source=wrong)
    assert refusal.value.code is PredecessorRefusal.WRONG_TREE
    assert _git(fixture.checkout, "rev-parse", "HEAD") == fixture.head
    assert _git(fixture.checkout, "show-ref") == fixture.refs


def test_noncommit_object_cannot_substitute_for_predecessor(
    squash_repository: SquashRepository,
) -> None:
    fixture = squash_repository
    tree = _git(fixture.checkout, "rev-parse", "HEAD^{tree}")
    wrong = ImmutablePredecessor(commit=tree, tree=tree)
    with pytest.raises(PredecessorError) as refusal:
        acquire_predecessor(repository=fixture.checkout, source=wrong)
    assert refusal.value.code is PredecessorRefusal.NOT_COMMIT
    assert not (fixture.checkout / ".git" / "FETCH_HEAD").exists()


@pytest.mark.parametrize("bad", ["main", "HEAD", "a" * 39, "A" * 40])
def test_pin_requires_exact_lowercase_object_id(bad: str) -> None:
    with pytest.raises(PredecessorError) as refusal:
        ImmutablePredecessor(commit=bad, tree="0" * 40)
    assert refusal.value.code is PredecessorRefusal.INVALID_PIN


def test_ci_and_postgresql_fixture_share_the_immutable_source_owner() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    fixture = (
        root / "tests/integration/test_billing_accounting_fact_export_pg.py"
    ).read_text(encoding="utf-8")
    assert "run: python3 -m scripts.ci.immutable_predecessor" in workflow
    assert "git merge-base --is-ancestor" not in workflow
    assert "repository=repository, source=ERP_ACCOUNTING_FACT_PREDECESSOR" in fixture
    assert "predecessor.source.commit" in fixture
    assert ERP_ACCOUNTING_FACT_PREDECESSOR.commit not in fixture
    assert 'assert "accounting_fact" not in old_labels' in fixture
    assert "cwd=root" in fixture  # Real historical migration subprocess retained.
