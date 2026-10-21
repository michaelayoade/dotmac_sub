"""Pure parser for RouterOS ``/export`` text (no I/O, no logging of content).

RouterOS 7 ``/export`` is compact: default values are omitted, and a property
that continues a dotted group is written as ``.name`` (for example
``local.address=1.2.3.4 .role=ebgp`` means ``local.role=ebgp``). The parser
joins ``\\`` continuation lines, tokenizes ``key=value`` pairs (quoted values,
``[ find ... ]`` selectors, ``!`` negated values) and reports how much of the
text it understood so callers can refuse to judge a snapshot it did not parse.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

MIN_PARSE_COVERAGE = 0.98

_VERSION_RE = re.compile(r"\bby RouterOS (\d+(?:\.\d+)*)")
_VERBS = frozenset({"add", "set", "remove"})
_TRUE = frozenset({"yes", "true"})
_FALSE = frozenset({"no", "false"})


class _Unparseable(ValueError):
    """Raised internally for a line the tokenizer cannot understand."""


def normalize_identity(value: str) -> str:
    """Trim, collapse whitespace and casefold a ``/system identity`` name."""
    return " ".join(value.split()).casefold()


def is_true(value: str | None) -> bool:
    return value is not None and value.lower() in _TRUE


def is_false(value: str | None) -> bool:
    return value is not None and value.lower() in _FALSE


@dataclass(frozen=True)
class ExportItem:
    section: str
    verb: str
    line: int
    positional: tuple[str, ...] = ()
    selector: Mapping[str, str] = field(default_factory=dict)
    props: Mapping[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.props.get(key, default)

    def has(self, key: str) -> bool:
        return key in self.props

    @property
    def enabled(self) -> bool:
        """RouterOS items are enabled unless ``disabled=yes`` is exported."""
        return not is_true(self.props.get("disabled"))

    def value_list(self, key: str) -> tuple[str, ...]:
        raw = self.props.get(key)
        if raw is None:
            return ()
        return tuple(part for part in raw.split(",") if part)


@dataclass(frozen=True)
class ExportDocument:
    version: str | None
    identity: str | None
    items: tuple[ExportItem, ...]
    total_lines: int
    tokenized_lines: int
    sections: frozenset[str]

    @property
    def coverage(self) -> float:
        if self.total_lines == 0:
            return 0.0
        return self.tokenized_lines / self.total_lines

    @property
    def parsed(self) -> bool:
        return self.identity is not None and self.coverage >= MIN_PARSE_COVERAGE

    def section(self, name: str) -> tuple[ExportItem, ...]:
        return tuple(item for item in self.items if item.section == name)

    def has_section(self, name: str) -> bool:
        return name in self.sections

    @property
    def version_tuple(self) -> tuple[int, ...] | None:
        return parse_version(self.version)


def parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """Read a quoted value starting at the opening quote."""
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise _Unparseable("unterminated quote")


def _read_word(text: str, start: int) -> tuple[str, int]:
    """Read a (possibly partially quoted) word ``key=value`` / bare word."""
    out: list[str] = []
    i = start
    while i < len(text) and not text[i].isspace():
        ch = text[i]
        if ch == '"':
            value, i = _read_quoted(text, i)
            out.append(value)
            continue
        out.append(ch)
        i += 1
    return "".join(out), i


def _split_pair(word: str) -> tuple[str, str] | None:
    if "=" not in word:
        return None
    key, _, value = word.partition("=")
    if not key:
        return None
    return key, value


def _tokenize(rest: str) -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    positional: list[str] = []
    selector: dict[str, str] = {}
    props: dict[str, str] = {}
    prefix = ""
    i = 0
    n = len(rest)
    while i < n:
        if rest[i].isspace():
            i += 1
            continue
        if rest[i] == "[":
            end = rest.find("]", i)
            if end < 0:
                raise _Unparseable("unterminated selector")
            inner = rest[i + 1 : end]
            j = 0
            while j < len(inner):
                if inner[j].isspace():
                    j += 1
                    continue
                word, j = _read_word(inner, j)
                if word in {"find", "where"}:
                    continue
                pair = _split_pair(word)
                if pair is None:
                    raise _Unparseable("bad selector token")
                selector[pair[0]] = pair[1]
            i = end + 1
            continue
        word, i = _read_word(rest, i)
        pair = _split_pair(word)
        if pair is None:
            positional.append(word)
            continue
        key, value = pair
        if key.startswith("."):
            key = f"{prefix}{key}" if prefix else key[1:]
        else:
            prefix = key.split(".", 1)[0] if "." in key else ""
        props[key] = value
    return tuple(positional), selector, props


def _join_continuations(text: str) -> list[tuple[int, str]]:
    """Return (first physical line number, joined logical line) pairs."""
    logical: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if buffer:
            buffer.append(line.lstrip())
        else:
            start = number
            buffer.append(line)
        if buffer[-1].endswith("\\"):
            buffer[-1] = buffer[-1][:-1]
            continue
        logical.append((start, "".join(buffer)))
        buffer = []
    if buffer:
        logical.append((start, "".join(buffer)))
    return logical


def parse_export(text: str) -> ExportDocument:
    """Parse RouterOS export text. Never raises on malformed input."""
    version: str | None = None
    identity: str | None = None
    items: list[ExportItem] = []
    sections: set[str] = set()
    section: str | None = None
    total = 0
    ok = 0
    for number, line in _join_continuations(text):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if version is None:
                match = _VERSION_RE.search(stripped)
                if match:
                    version = match.group(1)
            continue
        total += 1
        if stripped.startswith("/"):
            section = " ".join(stripped.split())
            sections.add(section)
            ok += 1
            continue
        verb, _, rest = stripped.partition(" ")
        if section is None or verb not in _VERBS:
            continue
        try:
            positional, selector, props = _tokenize(rest)
        except _Unparseable:
            continue
        ok += 1
        item = ExportItem(
            section=section,
            verb=verb,
            line=number,
            positional=positional,
            selector=selector,
            props=props,
        )
        items.append(item)
        if section == "/system identity" and verb == "set" and "name" in props:
            identity = props["name"]
    return ExportDocument(
        version=version,
        identity=identity,
        items=tuple(items),
        total_lines=total,
        tokenized_lines=ok,
        sections=frozenset(sections),
    )
