"""Shared analysis for the hard-coded credential guard.

A credential that is bound to a name in source is a credential that ships in
every clone, every image layer and every fork of the repository, and that no
rotation can reach. `scripts/one_off/send_reseller_welcome_email.py` was the
case that produced this guard: a module-level `TEMPORARY_PASSWORD` string
interpolated into an email body and mailed to two dozen external
organisations, none of whom had a per-account credential.

The detector matches SHAPE, never value. It carries no list of known-bad
strings, no entropy heuristic and no sample of anything that ever leaked --
a scanner that recognises a secret by its text has to store that secret to
work, which re-commits the thing it was written to remove.

The shape is: a binding whose NAME says "credential" and whose VALUE is a
string literal. A binding that reads the environment, calls a resolver, or
looks up a setting is a call node, not a constant, so it never matches; that
distinction is the whole test.

Two exclusions are built into the detector because they are properties of the
shape rather than opinions about a file:

* An empty or whitespace-only literal binds no credential.
* A literal that only restates words already present in its own identifier is
  a code, a table name or an enum member -- `CREDENTIALS = "user_credentials"`
  names a table, `api_key = "api_key"` names an enum member. Nothing is
  disclosed by a value that repeats its own name. The known cost of this rule
  is that a value which happens to be exactly a word from its identifier is
  not reported; this guard exists to stop real credentials reaching the
  tree, not to grade password strength.

DELIBERATELY UNMONITORED, so it is not mistaken for covered: dictionary-literal
entries (`{"password": ...}`), subscript assignments (`cfg["password"] = ...`),
non-Python entry points (shell, YAML, compose, Dockerfiles) and any value
assembled at runtime from parts. Those are regions this guard does not see,
not regions it has cleared.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from tests.architecture.source_index import python_ast, python_files

#: Entry-point families scanned. Every Python surface that can run against a
#: real deployment lives under one of these: application code and its Celery
#: tasks/workers (`app`), operator CLI, seeds, migrations-support and one-offs
#: (`scripts`), and schema migrations (`alembic`).
SCANNED_ROOTS: tuple[str, ...] = ("app", "scripts", "alembic")

#: `tests/` is scanned by nothing here, on a stated premise: test fixtures are
#: REQUIRED to construct credentials, and a fixture password is inert by
#: definition -- it authenticates against a disposable database created by the
#: test run itself. Weakening the detector so it could run over `tests/`
#: without noise would weaken it everywhere else, which is the trade this
#: exclusion refuses to make.
EXCLUDED_LANES: tuple[str, ...] = ("tests",)

#: An identifier names a credential when a credential word is its FINAL word.
#: `TEMPORARY_PASSWORD` holds one; `PASSWORD_RESET_COOKIE` and `token_type`
#: name something *about* one and hold none.
CREDENTIAL_IDENTIFIER = re.compile(
    r"(?:^|_)(?:"
    r"passwords?|passwds?|pwd|passphrases?"
    r"|secrets?|tokens?|credentials?|creds?"
    r"|api_?keys?|access_?keys?|secret_?keys?|private_?keys?"
    r"|signing_?keys?|auth_?keys?"
    r")$",
    re.IGNORECASE,
)

_WORD_BOUNDARY = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class CredentialLiteral:
    """One credential-named binding whose value is a string literal.

    Carries the location and the identifier only. The literal itself is never
    stored, returned or rendered into an assertion message: a guard must be
    safe to run in CI logs that anyone can read.
    """

    path: str
    lineno: int
    identifier: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} {self.identifier}"


def _words(text: str) -> frozenset[str]:
    return frozenset(part.lower() for part in _WORD_BOUNDARY.split(text) if part)


def _string_literal(node: ast.expr | None) -> str | None:
    """The constant string a value node evaluates to, if it is one.

    An f-string with no interpolation is still a literal. An f-string with a
    placeholder, a call, a name and a subscript are all not.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and all(
        isinstance(part, ast.Constant) and isinstance(part.value, str)
        for part in node.values
    ):
        return "".join(part.value for part in node.values)  # type: ignore[union-attr]
    return None


def binds_a_credential_literal(identifier: str, value: ast.expr | None) -> bool:
    """Whether this identifier/value pair is the shape the guard forbids."""

    if not identifier or not CREDENTIAL_IDENTIFIER.search(identifier):
        return False
    literal = _string_literal(value)
    if literal is None or not literal.strip():
        return False
    literal_words, identifier_words = _words(literal), _words(identifier)
    if not literal_words:
        return False
    # A value that only restates its own name discloses nothing.
    return not (literal_words <= identifier_words or identifier_words <= literal_words)


def _bindings(
    target: ast.expr, value: ast.expr | None
) -> list[tuple[str, ast.expr | None]]:
    """Pair each name a target binds with the value node it receives.

    Unpacking is paired element-wise when both sides are same-length
    sequences, so `SECRET_KEY, RETRIES = "...", 3` is seen as the credential
    binding it is rather than as an assignment of a tuple.
    """

    if isinstance(target, ast.Name):
        return [(target.id, value)]
    if isinstance(target, ast.Attribute):
        return [(target.attr, value)]
    if isinstance(target, ast.Tuple | ast.List):
        elements = (
            list(value.elts)
            if isinstance(value, ast.Tuple | ast.List)
            and len(value.elts) == len(target.elts)
            else [None] * len(target.elts)
        )
        return [
            binding
            for element, element_value in zip(target.elts, elements)
            for binding in _bindings(element, element_value)
        ]
    return []


def literals_in_tree(tree: ast.Module, path: str) -> tuple[CredentialLiteral, ...]:
    """Every credential-shaped binding in one parsed module."""

    found: list[CredentialLiteral] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for name, value in _bindings(target, node.value):
                    if binds_a_credential_literal(name, value):
                        found.append(CredentialLiteral(path, node.lineno, name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            for name, value in _bindings(node.target, node.value):
                if binds_a_credential_literal(name, value):
                    found.append(CredentialLiteral(path, node.lineno, name))
        elif isinstance(node, ast.Call):
            # `create_user(..., password="...")` binds a credential just as
            # surely as an assignment does.
            for keyword in node.keywords:
                if keyword.arg and binds_a_credential_literal(
                    keyword.arg, keyword.value
                ):
                    found.append(
                        CredentialLiteral(path, keyword.value.lineno, keyword.arg)
                    )
    return tuple(sorted(found, key=lambda hit: (hit.lineno, hit.identifier)))


def literals_in_source(
    source: str, path: str = "<constructed>"
) -> tuple[CredentialLiteral, ...]:
    """Analyse a source snippet. Used by the sensitivity proof."""

    return literals_in_tree(ast.parse(source, filename=path), path)


def literals_for(path: Path) -> tuple[CredentialLiteral, ...]:
    return literals_in_tree(python_ast(path), path.as_posix())


def scanned_files() -> tuple[Path, ...]:
    """Every Python file the guard reads.

    The scanned roots plus repository-root modules, which are entry points
    with no directory to belong to.
    """

    found: list[Path] = [path for path in sorted(Path().glob("*.py")) if path.is_file()]
    for root in SCANNED_ROOTS:
        found.extend(python_files(Path(root)))
    return tuple(found)


def counts_by_path() -> dict[str, int]:
    """Credential-shaped bindings per scanned file, for the ratchet."""

    counts: dict[str, int] = {}
    for path in scanned_files():
        hits = literals_for(path)
        if hits:
            counts[path.as_posix()] = len(hits)
    return counts
