"""One canonical form for cohort facts, so two systems can compare them.

A shadow importer will hold Sub's facts in a different database, a different
ORM and possibly a different runtime. "Did the data arrive intact?" is only
answerable if both sides can reduce a record to the same bytes. Everything in
this module exists to make that reduction total and boring.

## The rules, stated once

- **Field ordering** — keys sort by Unicode code point. Not declaration
  order: a model refactor that reorders attributes must not change a digest.
- **Collection ordering** — every exported collection is sorted before it is
  canonicalised. Row order out of a database is not a fact about the data.
- **Timezone** — every instant converts to UTC and renders with exactly six
  fractional digits and a trailing `Z`. A naive datetime is *refused*, never
  assumed to be UTC: guessing a timezone silently shifts a real timestamp.
- **Decimals** — normalised, then rendered in plain notation. `1.10` and `1.1`
  are the same number and must produce the same bytes; `1E+2` and `100` too.
- **Floats** — quantised to seven decimal places via `Decimal(repr(...))`.
  Only coordinates are stored as floats here, seven places is roughly a
  centimetre, and a float's last digit is not portable between runtimes.
- **Unicode** — NFC. Two spellings of one accented name must not read as two
  different customers. Nothing is stripped or case-folded: leading whitespace
  and letter case are data, and normalising them away would make the digest
  agree while the rows differed.
- **Null** — renders as `null` and is a value. A field that is absent cannot
  be distinguished from one that is null by a consumer, so absence is not
  allowed: every declared field appears in every record.
- **Omitted fields** — impossible by construction, for the reason above. A
  field can only be added or removed by a schema version bump.
- **Version evolution** — the contract version and the entity type are part of
  the digested payload. A v1 and a v2 record with byte-identical fields still
  produce different digests, so a version change can never look like data that
  happened not to move.

## What must never enter a canonical payload

Secret material, credentials, and unclassified free-text blobs. The first two
are not in the cohort's tables at all; the third is, and is deliberately
reduced to a key inventory plus a digest by the snapshot layer before it gets
here — see `snapshot.opaque_blob`.
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

#: The value shapes a canonical record may hold. Deliberately shallow: a
#: record is a flat mapping of scalars and scalar tuples, so canonicalisation
#: needs no recursion and no depth limit. Nested structure is flattened by the
#: snapshot layer into sorted strings before it reaches this module.
CanonicalScalar = str | int | bool | None
CanonicalField = CanonicalScalar | tuple[CanonicalScalar, ...]

#: Seven places ≈ 1.1 cm at the equator, which is finer than any plant record
#: Sub holds and coarse enough that no runtime's float printing can disagree.
COORDINATE_PLACES: Final[int] = 7

_ISO_MICROSECONDS: Final[str] = "%Y-%m-%dT%H:%M:%S.%f"


class CanonicalisationError(ValueError):
    """A value cannot be reduced to a comparable canonical form."""


class NaiveDatetimeError(CanonicalisationError):
    """A timestamp arrived without a timezone.

    Refused rather than assumed to be UTC. Sub stores every cohort timestamp
    as `DateTime(timezone=True)`, so a naive value means something upstream
    dropped the offset — and defaulting it here would move a real instant by
    up to a day and still digest cleanly.
    """


def canonical_string(value: str | None) -> str | None:
    """NFC-normalise a string, preserving everything else about it."""

    if value is None:
        return None
    return unicodedata.normalize("NFC", value)


def canonical_uuid(value: UUID | None) -> str | None:
    """Render a UUID in lowercase hyphenated form."""

    return None if value is None else str(value)


def canonical_datetime(value: datetime | None) -> str | None:
    """Render an aware instant as UTC with fixed microsecond precision."""

    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(
            f"{value!r} has no timezone. A cohort timestamp is stored aware; a "
            "naive one means an offset was dropped upstream, and assuming UTC "
            "here would move a real instant while still digesting cleanly."
        )
    return value.astimezone(UTC).strftime(_ISO_MICROSECONDS) + "Z"


def canonical_decimal(value: Decimal | None) -> str | None:
    """Render an exact decimal in plain notation, trailing zeros removed."""

    if value is None:
        return None
    try:
        normalised = value.normalize()
    except InvalidOperation as exc:  # pragma: no cover - NaN/Inf never stored
        raise CanonicalisationError(f"{value!r} is not a finite decimal") from exc
    if normalised == 0:
        # `Decimal("0.00").normalize()` is `Decimal("0")`, but
        # `Decimal("-0")` normalises to `Decimal("-0")`. One zero, one
        # spelling.
        return "0"
    text = format(normalised, "f")
    return text


def canonical_coordinate(value: float | None) -> str | None:
    """Quantise a stored coordinate to a portable fixed precision."""

    if value is None:
        return None
    quantum = Decimal(1).scaleb(-COORDINATE_PLACES)
    quantised = Decimal(repr(value)).quantize(quantum)
    return canonical_decimal(quantised)


def canonical_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    """NFC-normalise and sort a collection of strings."""

    return tuple(sorted(unicodedata.normalize("NFC", value) for value in values))


def _render(value: CanonicalField) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        # Checked before `int`: `bool` is a subclass of `int`, and rendering
        # True as `1` would make a flag indistinguishable from a count.
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, tuple):
        return "[" + ",".join(_render(item) for item in value) + "]"
    if isinstance(value, str):
        return _quote(value)
    raise CanonicalisationError(  # pragma: no cover - guarded by typing
        f"{type(value).__name__} has no canonical rendering; reduce it to a "
        "scalar or a tuple of scalars in the snapshot layer"
    )


def _quote(value: str) -> str:
    """Quote a string so no content can be confused with structure."""

    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def canonical_form(fields: dict[str, CanonicalField]) -> str:
    """Reduce a flat record to one comparable string.

    Written out rather than delegated to `json.dumps`: the separators, key
    ordering, escape set and `null`/`true` spellings are the contract, and a
    library default that changed between versions would silently change every
    digest ever computed.
    """

    parts = [f"{_quote(key)}:{_render(fields[key])}" for key in sorted(fields, key=str)]
    return "{" + ",".join(parts) + "}"


def canonical_digest(fields: dict[str, CanonicalField]) -> str:
    """SHA-256 of the canonical form, lowercase hex."""

    return hashlib.sha256(canonical_form(fields).encode("utf-8")).hexdigest()


def digest_of_text(value: str) -> str:
    """SHA-256 of one already-canonical string, lowercase hex."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "COORDINATE_PLACES",
    "CanonicalField",
    "CanonicalScalar",
    "CanonicalisationError",
    "NaiveDatetimeError",
    "canonical_coordinate",
    "canonical_datetime",
    "canonical_decimal",
    "canonical_digest",
    "canonical_form",
    "canonical_string",
    "canonical_strings",
    "canonical_uuid",
    "digest_of_text",
]
