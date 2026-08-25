"""Which modules write a column the composed inbox modules will own.

ADR-0013 § 3 splits `public.inbox_conversations` and `public.inbox_messages`
into PROJECTED columns (mirrors of `mod_inbox` facts, one writer: the
reconciler) and SUB-OWNED columns (Sub's own facts, written as they always
were). This census finds every module that assigns a projected column today.

The number is the cutover's remaining work, and the baseline beside it is a
two-directional ratchet: a module that starts writing a projected column fails
the guard, and so does a baselined module that stopped, until its line is
deleted in the same change that routed it through the module. The target is
one — `app/services/inbox_projection_reconciler.py` — and nothing else.

## Receivers are resolved, not guessed

`conversation.status = ...` is only counted when `conversation` is proven to
hold an `InboxConversation`. The proof is `isp_cohort_writers`' binding
machinery, reused rather than reimplemented: an annotated parameter, a
construction, a `db.get(...)`, or a query chain ending in a scalar terminal.

That reuse is the point. Two independent receiver resolvers in one repository
would disagree eventually, and the disagreement would be invisible — both would
report a number, and neither would be checkable against the other.

A name allowlist was NOT used, for the reason the subscriber-metadata census
records: `conversation` also names a `dotmac_inbox.models.Conversation` in this
very cutover's own code, and counting the module's own rows as Sub writes would
make the reconciler look like the problem it solves.

## Construction counts, and the earlier reasoning that said otherwise

An earlier version of this census deliberately ignored `InboxConversation(...)`
and `InboxMessage(...)` construction, arguing that a constructor is retired by
P6's schema change rather than by this ratchet.

**That argument is circular and the exclusion was wrong.** P6 runs AFTER
activation, so "P6 will remove the constructors" cannot be a premise for the P5
gate that authorises activation. With constructors invisible, this census could
reach an empty baseline — satisfying gate (e) — while every admission path still
created Sub-only rows the module had never heard of. At MODULE stage those rows
are orphans from the moment they are written.

So construction is counted, and it is counted SEPARATELY from field assignment,
because the two have different remedies: an assignment is routed through the
seam's mutation helpers, while a construction is replaced by
`inbox_writes.open_conversation` / `record_message`, which let the owner of the
moment mint the identity.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from scripts.architecture.isp_cohort_writers import (
    PROJECT_ROOT,
    _bound_cohort_model,
    _named_model,
    _query_chain_model,
    _scan_paths,
)

__all__ = [
    "MESSAGE_PROJECTED_COLUMNS",
    "CONVERSATION_PROJECTED_COLUMNS",
    "ProjectionWriteSite",
    "projection_write_sites",
    "projection_writer_files",
    "render_writer_baseline",
]

#: Sub column names the module owns after the switch. Mirrors the values of
#: `CONVERSATION_PROJECTION` / `MESSAGE_PROJECTION` in
#: `app/services/inbox_projection_reconciler.py`. A test asserts the two agree,
#: because a census watching a column the reconciler does not write — or missing
#: one it does — is a guard that reports a confident wrong number.
CONVERSATION_PROJECTED_COLUMNS = frozenset(
    {
        "channel_type",
        "status",
        "subject",
        "contact_address",
        "external_thread_id",
        "first_message_at",
        "last_message_at",
        "snoozed_until",
    }
)

MESSAGE_PROJECTED_COLUMNS = frozenset(
    {
        "channel_type",
        "direction",
        "subject",
        "body",
        "external_message_id",
        "sent_at",
        "received_at",
    }
)

_COLUMNS_BY_MODEL: dict[str, frozenset[str]] = {
    "InboxConversation": CONVERSATION_PROJECTED_COLUMNS,
    "InboxMessage": MESSAGE_PROJECTED_COLUMNS,
}

_MODELS = frozenset(_COLUMNS_BY_MODEL)

#: The two intended writers, and the only ones. `inbox_writes` is the runtime
#: seam that owns the LOCAL/SHADOW/MODULE branch; `inbox_projection_reconciler`
#: rebuilds the projection from the module afterwards. Both are named in
#: ADR-0013 and both are asserted to exist by the guard, so this exemption
#: cannot silently cover a file that was deleted or renamed.
#:
#: The backfill establishes history on the MODULE side and does not assign these
#: columns, so it is deliberately NOT exempted — if it ever appears here that is
#: a real finding, not an oversight.
_INTENDED_WRITERS = frozenset(
    {
        "app/services/inbox_writes.py",
        "app/services/inbox_projection_reconciler.py",
    }
)


@dataclass(frozen=True, slots=True, order=True)
class ProjectionWriteSite:
    """One module writing inbox rows, by assignment or by construction."""

    path: str
    count: int
    columns: tuple[str, ...]
    #: `InboxConversation` / `InboxMessage` constructions in this module. Kept
    #: apart from `columns` because the fix differs: a construction is replaced
    #: by a seam admission call, not by routing one field.
    constructions: tuple[str, ...] = ()


class _ProjectionWriteCounter(ast.NodeVisitor):
    """Count assignments to projected columns on a resolved inbox receiver."""

    def __init__(self) -> None:
        self.count = 0
        self.columns: set[str] = set()
        self.constructions: set[str] = set()
        self._bound: dict[str, str] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer = self._bound
        self._bound = dict(outer)
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ):
            annotated = _named_model(argument.annotation, _MODELS)
            if annotated is not None:
                self._bound[argument.arg] = annotated
        for child in node.body:
            self.visit(child)
        self._bound = outer

    def visit_Assign(self, node: ast.Assign) -> None:
        bound = _bound_cohort_model(node.value, _MODELS)
        if bound is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bound[target.id] = bound
        self._count_targets(node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        bound = _named_model(node.annotation, _MODELS) or (
            _bound_cohort_model(node.value, _MODELS) if node.value is not None else None
        )
        if isinstance(node.target, ast.Name) and bound is not None:
            self._bound[node.target.id] = bound
        self._count_targets([node.target])
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._count_targets([node.target])
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        iterated = _query_chain_model(node.iter, _MODELS)
        if isinstance(node.target, ast.Name) and iterated is not None:
            self._bound[node.target.id] = iterated
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Direct admission: `InboxConversation(...)` / `InboxMessage(...)`.

        Resolved through `_named_model` rather than by matching the bare name,
        so an unrelated class that happens to share a name is not a finding.
        """
        constructed = _named_model(node.func, _MODELS)
        if constructed is not None:
            self.count += 1
            self.constructions.add(constructed)
        self.generic_visit(node)

    def _count_targets(self, targets: list[ast.expr]) -> None:
        for target in targets:
            if not (
                isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            ):
                continue
            model = self._bound.get(target.value.id)
            if model is None:
                continue
            if target.attr in _COLUMNS_BY_MODEL[model]:
                self.count += 1
                self.columns.add(f"{model}.{target.attr}")


@cache
def projection_write_sites(
    *, project_root: Path = PROJECT_ROOT
) -> tuple[ProjectionWriteSite, ...]:
    """Every module assigning a projected inbox column, ordered by path."""

    sites: list[ProjectionWriteSite] = []
    for path in _scan_paths(project_root):
        relative = path.relative_to(project_root).as_posix()
        if relative in _INTENDED_WRITERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - lint gate
            continue
        counter = _ProjectionWriteCounter()
        counter.visit(tree)
        if counter.count:
            sites.append(
                ProjectionWriteSite(
                    path=relative,
                    count=counter.count,
                    columns=tuple(sorted(counter.columns)),
                    constructions=tuple(sorted(counter.constructions)),
                )
            )
    return tuple(sorted(sites))


def projection_writer_files(*, project_root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """The membership ratchet's input. Count-free on purpose.

    A writer appearing is a design question; a writer growing from three
    assignments to four is usually a refactor. One baseline holding both cannot
    say which happened, and a ratchet whose failure message is not the diagnosis
    is a ratchet people learn to regenerate without reading.
    """

    return tuple(
        site.path for site in projection_write_sites(project_root=project_root)
    )


def render_writer_baseline(*, project_root: Path = PROJECT_ROOT) -> str:
    header = (
        "# Projected inbox column WRITER baseline — membership only.\n"
        "#\n"
        "# One module path per line. Generated by\n"
        "# `python -m scripts.architecture.inbox_projection_census --baseline`.\n"
        "#\n"
        "# Two-directional. A module that starts writing a column the composed\n"
        "# inbox modules own fails the guard; so does a baselined module that\n"
        "# stopped, until its line is deleted in the same change that routed it\n"
        "# through `app/services/inbox_module/`.\n"
        "#\n"
        "# The target is ZERO, and an EMPTY baseline is part of the ADR-0013 P5\n"
        "# activation gate: while a line remains, activating would let the\n"
        "# reconciler overwrite a fact some other module still writes.\n"
        "#\n"
        "# The two intended writers — `app/services/inbox_writes.py` (the runtime\n"
        "# seam) and `app/services/inbox_projection_reconciler.py` — are excluded\n"
        "# by name and never appear here.\n"
        "#\n"
        "# What remains is blocked on FOUR named module gaps, not on effort.\n"
        "# Each line below is one of them; see ADR-0013 § 6a.\n"
        "#\n"
        "# Late delivery outcome — `sent_at` / `external_message_id` are stamped\n"
        "# after the provider accepts, but `record_message` takes the transport\n"
        "# ref at admission because it feeds the dedup key:\n"
        "#   app/services/communication_intents.py\n"
        "#   app/services/team_inbox_outbound.py   (queue-then-update upsert)\n"
        "#   app/tasks/notifications.py\n"
        "#\n"
        "# No external party — an internal channel has a Sub principal, not an\n"
        "# address, and the module has no typed internal identity. Admitting\n"
        "# them under an empty contact would make every anonymous session one\n"
        "# shared thread:\n"
        "#   app/services/team_inbox_field_job.py  (also needs provider thread\n"
        "#     identity on an INTERNAL transport, which ChannelSpec forbids)\n"
        "#   app/services/team_inbox_operations.py (internal note)\n"
        "\n"
    )
    return header + "".join(
        f"{path}\n" for path in projection_writer_files(project_root=project_root)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="write tests/architecture/inbox_projection_writers_baseline.txt",
    )
    args = parser.parse_args(argv)

    if args.baseline:
        target = (
            PROJECT_ROOT
            / "tests"
            / "architecture"
            / "inbox_projection_writers_baseline.txt"
        )
        target.write_text(render_writer_baseline(), encoding="utf-8")
        print(f"wrote {target.relative_to(PROJECT_ROOT)}")
        return 0

    for site in projection_write_sites():
        detail = ", ".join(site.columns)
        if site.constructions:
            built = ", ".join(f"{name}()" for name in site.constructions)
            detail = f"{detail} | {built}" if detail else built
        print(f"{site.count:>4}  {site.path}  {detail}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
