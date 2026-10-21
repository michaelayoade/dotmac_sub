# Invoice numbering candidate proof V2

Owner: `financial.billing_numbering_readiness`. The companion query
`app.services.billing_numbering_activation_proof.billing_numbering_activation_proof_report`
is delegated by that existing owner; it does not introduce another numbering owner.
V1 source, output shape, digests and CLI remain unchanged.

This is a read-only, invoice-only first-activation candidate proof. Credit notes
remain incumbent and their V1 census is bound through the legacy report digest.
A candidate result is never activation authorization. `activatable` is always
false. Target approval, legacy settings retirement, incumbent drain and sealed
single-writer flip, accepted inverse, and a verified released Kernel bridge
remain separately owed. No supplied boolean can waive these gates.

The caller supplies every released `SeriesConfiguration` field explicitly.
The CLI requires every field and accepts only literal true/false flags. No
production prefix or grammar is selected here. The bounded supported policy is
reset never, no year and no month. Length checks include the module's 32/32/8
storage limits and Sub's 80-character invoice limit for every legal value.

Released Numbering a2 formats nonempty prefix, decimal value padded to minimum
width, and nonempty suffix as separator-joined segments. For this supported
policy the fixed head is prefix plus separator when prefix is nonempty. The
legacy language is its exact prefix followed by positive padded ASCII digits.
A sufficient disjointness witness is either a mismatch within the two fixed
heads' shared positions, or a target head beginning with the legacy prefix and
containing a nondigit afterwards. All other relationships remain unproved.
Width comparisons and sampling are never proofs. The suffix alone is not used
to prove disjointness.

Every invoice UUID and raw label is scanned in UUID order against an independent
COUNT, including inactive, draft, void, null, empty and legacy OTHER labels.
Both grammars use the released public formatter for exact round-trip checking;
only exact legacy labels are counted as legacy replay. Null/empty/OTHER and
historical duplicates are observations, not repaired records. Any historical
label in the target grammar blocks first activation, including labels classified
OTHER by the legacy grammar. The complete ordered population is digest-bound.

All actual target configuration, counter and receipt columns are bound, including
identifiers, scope, timestamps and receipt idempotency keys. Raw labels and keys
are never emitted. Every existing receipt blocks this first-activation proof
pending separate reconciliation. A receipt's traceability key is not proof of
an idempotency-ledger or issued-document link. This query claims neither.

Missing configuration or a seeded counter is explicitly unmet. Public preview
is called only with an exact observed target configuration and present '*' counter;
the public API's missing-counter start-value fallback cannot satisfy this proof.
The observed counter must cover target start, effective legacy start, incumbent
next and exact legacy maximum plus one; an already-ahead target counter is preserved. Preview must
match the released formatter at that proven value. Actual preview is snapshot
evidence, never a reservation or a permission to allocate.

The query accepts only a clean caller-owned PostgreSQL REPEATABLE READ or
SERIALIZABLE READ ONLY session. It does not begin or complete transactions,
flush, lock, seed, configure, allocate, import, or repair. The standalone adapter
uses the existing read-only snapshot seam and disables/restores process-local
settings cache so canonical settings resolve against that snapshot. Freshness
is one snapshot; any changed input changes the digest. A future authorized
writer must repeat complete verification under incumbent exclusion and own the
inverse. No such writer is part of this change.

CLI exit 0 means complete disjoint candidate evidence; 1 means candidate refused;
2 means incomplete/unavailable evidence. Preparation blockers and operational
gates remain in the report even on exit 0. Hosted CI must run pure, architecture
and real-migrated PostgreSQL tests, including an independent committed writer,
unchanged catalogue and forbidden write attempts. Local validation is limited
to pinned Ruff and AST/static checks; no local runtime results are claimed.
