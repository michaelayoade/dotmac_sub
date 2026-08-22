"""Key-level census of ``subscribers.metadata`` access.

``subscribers.metadata`` is a JSONB blob with no declared shape, no schema and
no owner. Seven modules write it and many more read it, and its keys were added
by whichever feature needed somewhere to put something. The cohort writer
census (``isp_cohort_writers.py``) can say *which files* write the column; it
cannot say *what facts live in it*, and that is the question ownership needs.

This module answers it at key granularity: for every module that touches
``Subscriber.metadata_``, which keys it reads, writes or deletes.

## Why an AST rather than grep

``"latitude":`` appears in this repository hundreds of times, almost never as a
metadata key. A textual scan cannot tell a metadata subscript from any other
dict literal, and it cannot tell ``subscriber.metadata_`` from
``location_request.metadata_`` — a different model with a different blob. The
first draft of this census did exactly that and reported ``auto_decision`` as a
subscriber key when it belongs to ``CustomerLocationChangeRequest``.

## Receivers are RESOLVED, never guessed by name

``<x>.metadata_`` is counted only when ``x`` is proven to hold a ``Subscriber``.
The proof is a binding, borrowed from ``isp_cohort_writers``: an annotated
parameter or variable, a construction, a ``db.get(Subscriber, ...)``, or a
``query(Subscriber)`` chain ending in a scalar terminal.

A name allowlist was tried first and is the wrong instrument. Half this
codebase's receivers are generic — ``target``, ``existing``, ``record``,
``account`` — and each names a different model in a different module. Trusting
them reported twelve subscriber-metadata writers when there are seven, counting
``brand_profiles`` writing ``semantic_colors`` (a ``BrandProfile`` blob) and
``team_inbox_commands`` writing ``lead_capture`` (a conversation blob). A name
is not a type.

An unresolved receiver is **not** silently skipped. It lands in
``unclassified_receivers()``, which the guard fails on, because a writer hidden
behind an unresolvable binding is exactly what this census exists to find.

## What counts as an access

Four shapes, because the seven writers use all four:

1. ``subscriber.metadata_ = {...}`` — a dict literal, keys read from it.
2. ``meta = dict(subscriber.metadata_ or {})`` then ``meta["k"] = v`` — the
   copy-mutate-reassign pattern, which is what most of them do.
3. ``(subscriber.metadata_ or {}).get("k")`` — a direct read.
4. ``meta.pop("k")`` / ``del meta["k"]`` — deletion, which no writer does today
   and which the census reports separately so that stays visible.

A key that cannot be resolved to a literal is reported as ``<dynamic>`` rather
than dropped. ``<dynamic>`` is the finding, not a gap: a module that computes
its metadata keys at runtime has no declarable shape at all.

## Known limit: ``getattr``

``getattr(customer, "metadata_", None)`` is not counted. It is a reflective
read that no binding can resolve, and the one module using it —
``web_customer_details``, for a legacy ``latitude``/``longitude`` fallback — is
already counted as a reader through its other accesses, so the module set is
right even though those two keys are absent from the key inventory.

This is recorded rather than fixed because widening the census to reflective
access would mean matching on the attribute NAME again, which is the mistake
this module's receiver resolution exists to avoid. If a WRITE ever appears
behind ``getattr`` the module-level ratchet still catches it, because the same
module writes through a resolvable path or does not write at all.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]
APPLICATION_ROOT: Final = REPOSITORY_ROOT / "app"

#: The model, and the alias ``app/models/subscriber.py`` exports for it.
SUBSCRIBER_MODELS: Final[frozenset[str]] = frozenset(
    {"Subscriber", "SubscriberAccount"}
)

#: Receivers whose binding this census cannot resolve but which were READ and
#: proven to be a different model. Each entry is a decision, not a guess, and
#: the guard fails on anything absent from here and from a resolved binding —
#: so a new unresolvable receiver stops the build rather than shrinking the
#: census in silence.
#:
#: Nothing here is a subscriber. The three that WERE — `web_system_restore_tool`
#: iterating a purge query, `web_provisioning_bulk_activate` iterating
#: candidates, and `web_customer_actions`' five `before` bindings — were fixed
#: by teaching the resolver about loop targets and by annotating the product
#: code, never by listing them here. An entry in this set is a claim that the
#: subscriber column is not involved.
REVIEWED_FOREIGN_RECEIVERS: Final[frozenset[str]] = frozenset(
    {
        # Team Inbox — conversations, messages and their templates
        "InboxConversation",
        "InboxMessage",
        "message",
        "template",
        # Billing — invoices and their lines
        "Invoice",
        "invoice",
        "line",
        # Support and CRM tickets
        "Ticket",
        "TicketComment",
        "ticket",
        # Field and delivery work
        "WorkOrder",
        "Project",
        "task",
        "delivery",
        # Sales
        "duplicate",
        "quote",
        "edit_party",
        # Identity and audit
        "party",
        "event",
        # Payments
        "intent",
        # Branding and presentation
        "existing",
        "model",
    }
)

_SESSION_TOKENS: Final = frozenset({"db", "session", "db_session"})
_QUERY_TERMINALS: Final = frozenset(
    {"first", "one", "one_or_none", "scalar", "scalar_one", "scalar_one_or_none"}
)
#: Terminals yielding a COLLECTION of the model. `_bound_model` returns the
#: element type for these; only `visit_For` may use that, since binding a list
#: to the element name would be wrong anywhere else.
_COLLECTION_TERMINALS: Final = frozenset({"all", "scalars", "fetchall"})

_COPY_CALLS: Final = frozenset({"dict"})
_READ_METHODS: Final = frozenset({"get"})
_WRITE_METHODS: Final = frozenset({"setdefault", "update"})
_DELETE_METHODS: Final = frozenset({"pop"})

DYNAMIC: Final = "<dynamic>"


@dataclass(frozen=True, order=True)
class Access:
    """One resolved metadata access."""

    module: str
    operation: str
    key: str


def _module_name(path: Path) -> str:
    return str(path.relative_to(REPOSITORY_ROOT))


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` so ``_REASON_KEY`` resolves to a key."""

    constants: dict[str, str] = {}
    for node in tree.body:
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            constants[node.target.id] = value.value
    return constants


def _looks_like_model(name: str) -> bool:
    """``Subscriber``, ``InboxMessage``, ``Invoice`` — a class, not a local.

    Resolving EVERY model binding rather than only the subscriber's is what
    keeps `unclassified_receivers()` short enough to read. A receiver bound to
    `Invoice` is not "unknown"; it is known to be something else, and saying so
    is the difference between a 101-line list nobody reviews and the handful
    that genuinely need a human.
    """

    return bool(name) and name[0].isupper() and not name.isupper()


def _named_model(node: ast.expr | None) -> str | None:
    """The model an annotation or reference names, if any."""

    if node is None:
        return None
    if isinstance(node, ast.Name) and _looks_like_model(node.id):
        return node.id
    if isinstance(node, ast.Attribute) and _looks_like_model(node.attr):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _named_model(node.value)
    if isinstance(node, ast.BinOp):  # `Subscriber | None`
        return _named_model(node.left) or _named_model(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _looks_like_model(node.value) else None
    return None


def _receiver_tokens(node: ast.expr) -> set[str]:
    tokens: set[str] = set()
    current: ast.expr | None = node
    while current is not None:
        if isinstance(current, ast.Name):
            tokens.add(current.id)
            break
        if isinstance(current, ast.Attribute):
            tokens.add(current.attr)
            current = current.value
            continue
        if isinstance(current, ast.Call):
            current = current.func
            continue
        break
    return tokens


def _query_chain_model(node: ast.expr) -> str | None:
    """The model a query expression selects.

    Searches the whole subtree rather than the direct arguments of each call in
    the chain. `db.scalars(select(Subscriber).where(...).where(...)).all()`
    nests the model two calls deep inside one argument, and walking only the
    spine misses it — which is how the purge sweep's writer stayed invisible.
    Searching a query expression this broadly is safe: the only model names
    inside one are the models it queries.
    """

    for child in ast.walk(node):
        if isinstance(child, ast.Name | ast.Attribute):
            named = _named_model(child)
            if named is not None:
                return named
    return None


#: ``{function name: model it returns}``, built once across the application.
#: A local bound from a call is otherwise unresolvable, and the calls that
#: matter cross module boundaries — `crm_customer_name_repair` writes subscriber
#: metadata onto the return of `web_customer_actions.approve_subscriber_name_
#: correction`, which is annotated `-> Subscriber` three files away. Without
#: this index that write is invisible, which is the whole failure mode the
#: census exists to prevent.
_RETURN_INDEX: dict[str, str] = {}


#: Module-level functions whose whole body is ``return dict(<sub>.metadata_ …)``.
#: `web_system_restore_tool` routes every access through one of these, so
#: without them its four `metadata.pop(...)` deletions and its whole-blob writes
#: are invisible — the census reported "no writer deletes a key today", which
#: was false.
_COPY_HELPERS: set[str] = set()


def _is_metadata_copy_helper(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [item for item in node.body if not isinstance(item, ast.Expr)]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    value = body[0].value
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id in _COPY_CALLS and value.args:
            value = value.args[0]
    if isinstance(value, ast.BoolOp) and value.values:
        value = value.values[0]
    return isinstance(value, ast.Attribute) and value.attr == "metadata_"


def _build_return_index(paths: list[Path]) -> dict[str, str]:
    index: dict[str, str] = {}
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - the repo does not hold one
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _is_metadata_copy_helper(node):
                _COPY_HELPERS.add(node.name)
            returned = _named_model(node.returns)
            if returned is None:
                continue
            # A name defined twice with different return models is ambiguous;
            # drop it rather than let one definition speak for the other.
            if index.get(node.name, returned) != returned:
                index[node.name] = ""
            else:
                index[node.name] = returned
    return {name: model for name, model in index.items() if model}


def _bound_model(value: ast.expr) -> str | None:
    """The model an expression yields an instance of, if any.

    Construction, `db.get(Model, ...)`, and a `query(Model)` chain ending in a
    scalar terminal. Anything else is unresolved, which the caller reports
    rather than assumes.
    """

    if isinstance(value, ast.Await):
        return _bound_model(value.value)
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    constructed = _named_model(func)
    if constructed is not None:
        return constructed
    if isinstance(func, ast.Attribute):
        if func.attr == "get" and (_receiver_tokens(func.value) & _SESSION_TOKENS):
            for argument in value.args:
                named = _named_model(argument)
                if named is not None:
                    return named
            return None
        if func.attr in _QUERY_TERMINALS | _COLLECTION_TERMINALS:
            return _query_chain_model(func.value)
        if func.attr in _RETURN_INDEX:
            return _RETURN_INDEX[func.attr]
    if isinstance(func, ast.Name) and func.id in _RETURN_INDEX:
        return _RETURN_INDEX[func.id]
    return None


class _Census(ast.NodeVisitor):
    def __init__(self, module: str, constants: dict[str, str]) -> None:
        self.module = module
        self.constants = constants
        self.accesses: list[Access] = []
        self.unclassified: set[str] = set()
        #: locals currently holding a copy of a subscriber's metadata dict
        self._aliases: set[str] = set()
        #: local name -> model class it was proven to hold, per function scope
        self._bindings: dict[str, str] = {}

    def _visit_scope(self, node: ast.AST, arguments: ast.arguments) -> None:
        outer_bindings, outer_aliases = self._bindings, self._aliases
        self._bindings, self._aliases = dict(outer_bindings), set()
        every = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            arguments.vararg,
            arguments.kwarg,
        ]
        for argument in every:
            if argument is None:
                continue
            annotated = _named_model(argument.annotation)
            if annotated is not None:
                self._bindings[argument.arg] = annotated
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._bindings, self._aliases = outer_bindings, outer_aliases

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.args)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.args)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            annotated = _named_model(node.annotation) or (
                _bound_model(node.value) if node.value is not None else None
            )
            if annotated is not None:
                self._bindings[node.target.id] = annotated
        self.generic_visit(node)

    # -- receiver classification -------------------------------------------

    def _is_subscriber_metadata(self, node: ast.expr) -> bool:
        """True for ``<subscriber>.metadata_``, by resolved binding.

        `Subscriber.metadata_` — the CLASS attribute — is a SQL expression in a
        query filter, not an instance access. It reads the column and is
        counted as a read of an unresolvable key rather than ignored.
        """

        if not (isinstance(node, ast.Attribute) and node.attr == "metadata_"):
            return False
        receiver = node.value
        if isinstance(receiver, ast.Name):
            if receiver.id in SUBSCRIBER_MODELS:
                return True
            model = self._bindings.get(receiver.id)
            if model is not None:
                return model in SUBSCRIBER_MODELS
            if receiver.id in REVIEWED_FOREIGN_RECEIVERS:
                return False
            self.unclassified.add(f"{self.module}:{receiver.id}")
            return False
        if isinstance(receiver, ast.Attribute):
            # `order.line.metadata_`: the binding is outside this function, so
            # only the attribute name is available. It is checked against the
            # same reviewed set, and reported when absent.
            if receiver.attr in REVIEWED_FOREIGN_RECEIVERS:
                return False
            self.unclassified.add(f"{self.module}:<attribute {receiver.attr}>")
            return False
        self.unclassified.add(f"{self.module}:<expression>")
        return False

    def _unwrap(self, node: ast.expr) -> ast.expr:
        """See through ``dict(...)`` and ``... or {}`` to the real receiver."""

        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _COPY_CALLS and node.args:
                return self._unwrap(node.args[0])
        if isinstance(node, ast.BoolOp) and node.values:
            return self._unwrap(node.values[0])
        return node

    def _key(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self.constants[node.id]
        if isinstance(node, ast.Attribute) and node.attr in self.constants:
            return self.constants[node.attr]
        return DYNAMIC

    def _record(self, operation: str, key: str) -> None:
        self.accesses.append(Access(self.module, operation, key))

    def _is_alias(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id in self._aliases

    # -- visits -------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        bound = _bound_model(node.value)
        if bound is not None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bindings[target.id] = bound
        # `a = b` propagates a proven binding along an alias chain.
        if isinstance(node.value, ast.Name) and node.value.id in self._bindings:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bindings[target.id] = self._bindings[node.value.id]

        source = self._unwrap(node.value)
        is_copy_helper = (
            isinstance(source, ast.Call)
            and isinstance(source.func, ast.Name)
            and source.func.id in _COPY_HELPERS
        )
        if is_copy_helper or self._is_subscriber_metadata(source):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._aliases.add(target.id)

        for target in node.targets:
            if self._is_subscriber_metadata(target):
                self._record_whole_assignment(node.value)
            elif isinstance(target, ast.Subscript) and self._is_alias(target.value):
                self._record("write", self._key(target.slice))
        self.generic_visit(node)

    def _record_whole_assignment(self, value: ast.expr) -> None:
        """``subscriber.metadata_ = <value>`` — what shape is being stored?"""

        if isinstance(value, ast.Dict):
            for key in value.keys:
                if key is None:  # ``**spread`` carries no new key of its own
                    continue
                self._record("write", self._key(key))
            return
        if self._is_alias(value):
            # Reassigning the mutated copy back. Its keys were already counted
            # at the subscript writes that produced them.
            return
        # A whole blob from somewhere this census cannot see the shape of.
        self._record("write", DYNAMIC)

    def visit_For(self, node: ast.For) -> None:
        """`for subscriber in db.scalars(select(Subscriber)).all():`

        A loop over a model query is how the purge sweep reaches every row, and
        it was the last shape hiding a real writer from this census.
        """

        iterated = _bound_model(node.iter)
        if iterated is None and isinstance(node.iter, ast.Name):
            # The query is usually assigned first and iterated a line later:
            # `candidates = db.scalars(...).all()` then `for subscriber in
            # candidates:`. Both halves have to be followed, or the loop body
            # looks like it operates on an unknown type.
            iterated = self._bindings.get(node.iter.id)
        if iterated is not None and isinstance(node.target, ast.Name):
            self._bindings[node.target.id] = iterated
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and (
            self._is_alias(node.value) or self._is_subscriber_metadata(node.value)
        ):
            self._record("read", self._key(node.slice))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and node.args:
            base = self._unwrap(func.value)
            if self._is_alias(base) or self._is_subscriber_metadata(base):
                if func.attr in _READ_METHODS:
                    self._record("read", self._key(node.args[0]))
                elif func.attr in _DELETE_METHODS:
                    self._record("delete", self._key(node.args[0]))
                elif func.attr in _WRITE_METHODS:
                    self._record("write", self._key(node.args[0]))
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript) and self._is_alias(target.value):
                self._record("delete", self._key(target.slice))
        self.generic_visit(node)


def _imports_subscriber(tree: ast.Module) -> bool:
    """Can this module hold a ``Subscriber`` at all?

    A module that never imports the model cannot bind one, so its `metadata_`
    receivers belong to some other model by construction. Filtering on the
    import is what makes `unclassified_receivers()` a usable guard instead of
    a list of every JSON blob in the application: without it the check reports
    199 receivers, nearly all of them inbox conversations and VPN peers, and
    nobody would read it.
    """

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name in SUBSCRIBER_MODELS for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name in SUBSCRIBER_MODELS for alias in node.names):
                return True
    return False


def _python_files() -> list[Path]:
    return sorted(
        path
        for path in APPLICATION_ROOT.rglob("*.py")
        # The migration-source inventory DESCRIBES this column in prose and
        # classification data; it never touches a live row.
        if "migration_source" not in path.parts
    )


def _scan() -> tuple[list[Access], set[str]]:
    accesses: list[Access] = []
    unclassified: set[str] = set()
    paths = _python_files()
    global _RETURN_INDEX
    if not _RETURN_INDEX:
        _RETURN_INDEX = _build_return_index(paths)
    for path in paths:
        source = path.read_text(encoding="utf-8")
        if "metadata_" not in source:
            continue
        tree = ast.parse(source)
        if not _imports_subscriber(tree):
            continue
        census = _Census(_module_name(path), _string_constants(tree))
        # Two passes: an alias can be established after its first textual use
        # inside a long module, and the visitor is single-pass by nature.
        census.visit(tree)
        # Both results are discarded and recomputed. Clearing only `accesses`
        # kept every first-pass receiver that had not been bound YET, so a
        # perfectly resolvable `subscriber = db.get(Subscriber, ...)` was
        # reported unclassified purely because of walk order.
        census.accesses.clear()
        census.unclassified.clear()
        census.visit(tree)
        accesses.extend(census.accesses)
        unclassified |= census.unclassified
    return accesses, unclassified


def metadata_accesses() -> list[Access]:
    """Every resolved ``subscribers.metadata`` access, deduplicated."""

    accesses, _ = _scan()
    return sorted(set(accesses))


def unclassified_receivers() -> list[str]:
    """``<name>.metadata_`` receivers that are neither subscriber nor foreign.

    The load-bearing half. A writer hidden behind an unrecognised variable name
    escapes every other check in this module, so the guard fails on a non-empty
    result rather than reporting a smaller census.
    """

    _, unclassified = _scan()
    return sorted(unclassified)


def metadata_writers() -> set[str]:
    """Modules that WRITE the column — the set the ratchet counts."""

    return {
        access.module
        for access in metadata_accesses()
        if access.operation in {"write", "delete"}
    }


def metadata_readers() -> set[str]:
    """Modules that only read it. Item 6's compatibility-projection set."""

    writers = metadata_writers()
    return {
        access.module
        for access in metadata_accesses()
        if access.operation == "read" and access.module not in writers
    }


def keys_by_module() -> dict[str, dict[str, set[str]]]:
    """``{module: {operation: {key, ...}}}``."""

    table: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for access in metadata_accesses():
        table[access.module][access.operation].add(access.key)
    return {module: dict(operations) for module, operations in table.items()}


def render_writer_baseline() -> str:
    lines = [
        "# `subscribers.metadata` direct-WRITER baseline — membership only.",
        "#",
        "# One module path per line. Generated by",
        "# `python -m scripts.architecture.subscriber_metadata_census --baseline`.",
        "#",
        "# Two-directional. A module that starts writing the column fails the",
        "# guard; so does a baselined module that stopped, until its line is",
        "# deleted in the same change that routed it through an owner.",
        "#",
        "# The column has no declared shape and no owner. Every line here is a",
        "# feature storing a fact somewhere nothing constrains, and the target",
        "# is zero — see docs/SUBSCRIBER_METADATA_OWNERSHIP.md for the classified",
        "# key census and the owner each retained fact is assigned to.",
        "",
    ]
    lines.extend(sorted(metadata_writers()))
    return "\n".join(lines) + "\n"


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="emit the ratchet")
    parser.add_argument("--json", action="store_true", help="emit the full census")
    arguments = parser.parse_args()

    if arguments.baseline:
        print(render_writer_baseline(), end="")
        return 0

    if arguments.json:
        payload = {
            "writers": sorted(metadata_writers()),
            "readers": sorted(metadata_readers()),
            "unclassified_receivers": unclassified_receivers(),
            "keys": {
                module: {
                    operation: sorted(keys) for operation, keys in operations.items()
                }
                for module, operations in sorted(keys_by_module().items())
            },
        }
        print(json.dumps(payload, indent=2))
        return 0

    writers = metadata_writers()
    print(f"direct writers: {len(writers)}")
    for module in sorted(writers):
        operations = keys_by_module()[module]
        for operation in ("write", "delete", "read"):
            keys = sorted(operations.get(operation, ()))
            if keys:
                print(f"  {module}\n    {operation}: {', '.join(keys)}")
    readers = metadata_readers()
    print(f"\nread-only modules: {len(readers)}")
    for module in sorted(readers):
        keys = sorted(keys_by_module()[module].get("read", ()))
        print(f"  {module}: {', '.join(keys)}")
    unclassified = unclassified_receivers()
    if unclassified:
        print(f"\nUNCLASSIFIED RECEIVERS ({len(unclassified)}):")
        for name in unclassified:
            print(f"  {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI adapter
    raise SystemExit(_main())
