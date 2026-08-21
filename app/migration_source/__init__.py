"""Sub as the measured *source* of the ISP replacement — never as its target.

This package is governance and read-only contract. Nothing in it is imported
by Sub's request path today, nothing in it writes, and nothing in it decides
anything about the destination application. It exists so that three questions
have mechanical answers instead of recollections:

1. which Governance revision binds Sub to this programme (`programme`);
2. which Sub tables hold the cohort's source facts (`cohort`);
3. what every surface that writes them actually is (`surfaces`).

## Why it lives under `app/`

`make check` points ruff, mypy and bandit at `app/`. A typed contract placed
anywhere else would leave the repository's own rule — public inputs and
outputs are strongly typed, never `Any` or an unshaped dict — unenforced by
the gate meant to prove it. Living here means the contract is checked by the
existing gates with no Makefile change. `app/shadow/` is here for the same
reason and is a different concern: that package describes a disposable shadow
*environment*; this one describes Sub as a migration *source*.

## The boundary this package must not cross

Sub is `asm-dotmac-sub-legacy` and remains the sole production writer of every
cohort fact until a separately authorised sealed switch. This package composes
no Starter module, adds no migration, opens no cohort, writes to no target
database, and advances no Governance control. Those limits are structural
where they can be — the vocabularies simply lack the words — and stated here
where they cannot be.
"""

from __future__ import annotations

from app.migration_source.cohort import (
    COHORT_TABLES,
    CohortComponent,
    CohortEntityType,
    CohortTable,
    cohort_model_names,
    cohort_table_names,
    cohort_tables_by_entity,
)
from app.migration_source.programme import (
    ACCEPTED_REVISION,
    BINDING,
    CLAIMS,
    COHORT_ID,
    CohortState,
    GovernanceBinding,
    SourceReadinessClaim,
)
from app.migration_source.surfaces import (
    COHORT_SURFACES,
    TABLES_WITH_NO_COUNTED_WRITER,
    UNMAPPED_ADJACENT_TABLES,
    SourceSurface,
    SurfaceClassification,
    production_writer_paths,
    surfaces_by_classification,
)

__all__ = [
    "ACCEPTED_REVISION",
    "BINDING",
    "CLAIMS",
    "COHORT_ID",
    "COHORT_SURFACES",
    "COHORT_TABLES",
    "TABLES_WITH_NO_COUNTED_WRITER",
    "UNMAPPED_ADJACENT_TABLES",
    "CohortComponent",
    "CohortEntityType",
    "CohortState",
    "CohortTable",
    "GovernanceBinding",
    "SourceReadinessClaim",
    "SourceSurface",
    "SurfaceClassification",
    "cohort_model_names",
    "cohort_table_names",
    "cohort_tables_by_entity",
    "production_writer_paths",
    "surfaces_by_classification",
]
