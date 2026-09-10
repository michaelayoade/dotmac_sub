"""Every strict-JSON evidence document under docs/ must parse as JSON.

Michael's ruling on this repair: "strict JSON evidence files must be outside
Ruff's formatting ownership, while their validator runs after formatting and
fails on invalid JSON. Don't 'fix' this by accepting Ruff-formatted
pseudo-JSON."

``ruff format`` rewrites ``docs/kernel-runtime-composition.json`` into
pseudo-JSON with a trailing comma before the closing ``}``/``]`` — confirmed
directly against this repository (copy the file, run ``ruff format`` on the
copy, and ``json.load`` the result: ``json.decoder.JSONDecodeError: Illegal
trailing comma before end of object``). ``docs/kernel-runtime-readiness.json``,
untouched by the change that added this test, shows the identical
"Would reformat" from ``ruff format --check`` — this is pre-existing and
formatter-shaped, not specific to one document's content.

The repair has two parts. ``pyproject.toml``'s ``[tool.ruff] extend-exclude``
and ``.pre-commit-config.yaml``'s per-hook ``exclude:`` (a directory-walk
exclude does not help against a pre-commit hook that passes a changed file's
exact path) keep the formatter off both documents entirely. THIS test is the
other half: it runs ``json.load`` directly against both documents and fails,
naming the path, on any parse error — so if the formatter's exclusion is ever
removed or bypassed, this test (not a human reading a diff) is what catches
the corruption. Order matters: this must run as part of the normal test
suite, never a "does ruff accept it" check that a corrupting formatter step
could run before and mask.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"

#: The exact set of strict-JSON evidence documents this validator owns.
#: Adding a new one is a deliberate change to this tuple, not something a
#: glob silently discovers or silently misses.
STRICT_JSON_EVIDENCE_DOCUMENTS = (
    DOCS_ROOT / "kernel-runtime-composition.json",
    DOCS_ROOT / "kernel-runtime-readiness.json",
)


@pytest.mark.parametrize("path", STRICT_JSON_EVIDENCE_DOCUMENTS, ids=lambda p: p.name)
def test_strict_json_evidence_document_parses(path: Path) -> None:
    assert path.is_file(), f"{path} does not exist"
    try:
        with path.open(encoding="utf-8") as handle:
            json.load(handle)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{path} is not valid JSON: {exc}. If this started failing after "
            "a formatter ran over it, the fix is to exclude the document "
            "from the formatter's ownership (pyproject.toml [tool.ruff] "
            "extend-exclude and .pre-commit-config.yaml's per-hook "
            "exclude), never to accept the formatter's output."
        ) from exc


def test_a_trailing_comma_document_is_refused_by_this_validator() -> None:
    """Sensitivity plant: without this case, the parametrized test above
    passes on a clean tree for the wrong reason — it would pass identically
    whether or not it is actually capable of catching a malformed document.
    A synthetic document shaped exactly like ruff-format's known output
    (a trailing comma before the closing brace) must fail json.load."""
    trailing_comma_pseudo_json = (
        '{\n  "schema_version": "dimensional-composition.v2",\n}\n'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(trailing_comma_pseudo_json)


def test_strict_json_evidence_documents_are_excluded_from_ruff_formatting() -> None:
    """Static proof that the formatter-ownership half of the repair is in
    place, not just the validator half: pyproject.toml's [tool.ruff]
    extend-exclude must name both documents by their exact repository-
    relative path (not a docs/ wildcard — this repair is scoped to strict-
    JSON evidence documents, not documentation generally)."""
    import tomllib

    pyproject = tomllib.loads(
        (DOCS_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    excluded = set(pyproject["tool"]["ruff"].get("extend-exclude", ()))
    for path in STRICT_JSON_EVIDENCE_DOCUMENTS:
        relative = path.relative_to(DOCS_ROOT.parent).as_posix()
        assert relative in excluded, (
            f"{relative} must be named exactly in [tool.ruff] extend-exclude"
        )
    # Scoped to the two documents, not docs/ wholesale.
    assert "docs" not in excluded
    assert "docs/" not in excluded
