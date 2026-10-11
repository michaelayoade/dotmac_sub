"""Invocation-scoped SQLAlchemy evidence for protected auth owner unit tests.

No SQL text, parameters, mapped values, credential or identity is retained.
Session roots are logical owner transactions, including fixture savepoints.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.sql.dml import Insert

AUTH_PROTECTED_TABLES = frozenset({"user_credentials", "sessions"})
EXTERNAL_INTENT_TABLES = frozenset({"event_store"})


class FactKind(StrEnum):
    BEGIN = "begin"
    SQL_WRITE = "sql_write"
    MAPPER_WRITE = "mapper_write"
    COMMIT = "commit"
    ROLLBACK = "rollback"
    END = "end"
    EXTERNAL = "external"


@dataclass(frozen=True)
class TraceFact:
    order: int
    kind: FactKind
    root: int | None
    transaction: int | None
    table: str | None = None
    operation: str | None = None
    intent_root: int | None = None
    active_roots: tuple[int, ...] = ()
    evidence_insert: bool = False


@dataclass(frozen=True)
class Transaction:
    number: int
    root: int
    parent: int | None


@dataclass(frozen=True)
class TraceResult:
    transactions: tuple[Transaction, ...]
    facts: tuple[TraceFact, ...]


class TransactionTrace:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._transactions: dict[SessionTransaction, Transaction] = {}
        self._connections: dict[Connection, list[SessionTransaction]] = {}
        self._facts: list[TraceFact] = []
        self._active = False
        self._snapshot: TraceResult | None = None

    @property
    def result(self) -> TraceResult:
        if self._snapshot is not None:
            return self._snapshot
        return TraceResult(tuple(self._transactions.values()), tuple(self._facts))

    def _transaction(self, tx: SessionTransaction) -> Transaction:
        if tx not in self._transactions:
            parent = self._transaction(tx.parent) if tx.parent else None
            number = len(self._transactions) + 1
            self._transactions[tx] = Transaction(
                number,
                parent.root if parent else number,
                parent.number if parent else None,
            )
        return self._transactions[tx]

    def _record(
        self,
        kind: FactKind,
        tx: SessionTransaction | None,
        *,
        table: str | None = None,
        operation: str | None = None,
        intent_root: int | None = None,
        active_roots: tuple[int, ...] = (),
        evidence_insert: bool = False,
    ) -> None:
        transaction = self._transaction(tx) if tx else None
        self._facts.append(
            TraceFact(
                len(self._facts) + 1,
                kind,
                transaction.root if transaction else None,
                transaction.number if transaction else None,
                table,
                operation,
                intent_root,
                active_roots,
                evidence_insert,
            )
        )

    def _matches(self, session: Session) -> bool:
        bind = session.get_bind()
        return (bind.engine if isinstance(bind, Connection) else bind) is self.engine

    def _begin(
        self, session: Session, tx: SessionTransaction, connection: Connection
    ) -> None:
        if connection.engine is self.engine:
            self._connections.setdefault(connection, []).append(tx)
            self._record(FactKind.BEGIN, tx)

    def _completion(self, session: Session, kind: FactKind) -> None:
        if self._matches(session):
            tx = session.get_nested_transaction() or session.get_transaction()
            if tx:
                self._record(kind, tx)
                if kind == FactKind.COMMIT and tx.parent is None:
                    for connection, stack in tuple(self._connections.items()):
                        self._connections[connection] = [
                            item
                            for item in stack
                            if self._transaction(item).root
                            != self._transaction(tx).root
                        ]

    def _commit(self, session: Session) -> None:
        self._completion(session, FactKind.COMMIT)

    def _rollback(self, session: Session) -> None:
        self._completion(session, FactKind.ROLLBACK)

    def _end(self, session: Session, tx: SessionTransaction) -> None:
        if tx in self._transactions:
            self._record(FactKind.END, tx)
            for connection, stack in tuple(self._connections.items()):
                self._connections[connection] = [
                    item for item in stack if item is not tx
                ]
                if not self._connections[connection]:
                    del self._connections[connection]

    def _flush(
        self, session: Session, flush_context: object, instances: object
    ) -> None:
        if not self._matches(session):
            return
        tx = session.get_nested_transaction() or session.get_transaction()
        for operation, entities in (
            ("insert", session.new),
            ("update", session.dirty),
            ("delete", session.deleted),
        ):
            for entity in entities:
                table = inspect(type(entity)).local_table.name
                self._record(
                    FactKind.MAPPER_WRITE, tx, table=table, operation=operation
                )

    def _sql(
        self,
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        # Only anchored DML is supported; deliberately ignore SELECT/set_config.
        # Unknown/CTE DML is not coverage: the runbook requires a new parser test.
        match = re.match(
            r"^\s*(INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\s+"
            r'(?:(?:"?[\w]+"?)\.)?"?([\w]+)"?',
            statement,
            re.I,
        )
        if match:
            stack = self._connections.get(connection, [])
            operation = match[1].split()[0].lower()
            # after_cursor_execute excludes failures. Conflict/ignore/replace and
            # INSERT SELECT are outside evidence proof, even if they affect rows.
            # SQLite reports rowcount=0 before fetching ordinary RETURNING rows.
            plain_values_insert = (
                operation == "insert"
                and re.match(r"^\s*INSERT\s+INTO\b", statement, re.I) is not None
                and re.search(r"\bVALUES\s*\(", statement, re.I) is not None
                and re.search(r"\bON\s+CONFLICT\b", statement, re.I) is None
            )
            compiled = getattr(context, "compiled", None)
            compiled_statement = getattr(compiled, "statement", None)
            # Literal SQL tokens never establish RETURNING semantics. Only a
            # compiled VALUES Insert with the compiler's actual RETURNING list
            # can use SQLite's pre-fetch rowcount exception.
            compiled_values_returning = (
                isinstance(compiled_statement, Insert)
                and compiled_statement.select is None
                and bool(getattr(compiled, "returning", ()))
            )
            evidence_insert = plain_values_insert and (
                getattr(cursor, "rowcount", -1) > 0 or compiled_values_returning
            )
            self._record(
                FactKind.SQL_WRITE,
                stack[-1] if stack else None,
                table=match[2],
                operation=operation,
                evidence_insert=evidence_insert,
            )

    def record_external_effect(self, *, intent_root: int) -> None:
        """Call from a bounded transport spy; takes no payload or identity."""
        if not self._active:
            raise RuntimeError("external effect outside trace invocation")
        active_roots = tuple(
            sorted(
                {
                    self._transaction(stack[-1]).root
                    for stack in self._connections.values()
                    if stack
                }
            )
        )
        self._record(
            FactKind.EXTERNAL, None, intent_root=intent_root, active_roots=active_roots
        )


@contextmanager
def trace_transactions(engine: Engine) -> Iterator[TransactionTrace]:
    """Bind after fixture setup. Always detach, including on owner exceptions."""
    trace = TransactionTrace(engine)
    hooks = (
        (Session, "after_begin", trace._begin),
        (Session, "after_commit", trace._commit),
        (Session, "after_rollback", trace._rollback),
        (Session, "after_transaction_end", trace._end),
        (Session, "before_flush", trace._flush),
        (engine, "after_cursor_execute", trace._sql),
    )
    try:
        for target, name, callback in hooks:
            if target is Session:
                event.listen(target, name, callback, insert=True)
            else:
                event.listen(target, name, callback)
        trace._active = True
        yield trace
    finally:
        trace._active = False
        for target, name, callback in reversed(hooks):
            if event.contains(target, name, callback):
                event.remove(target, name, callback)
        trace._snapshot = trace.result
        # Retain only immutable facts, never owner Session/transaction objects.
        trace._transactions.clear()
        trace._connections.clear()


def assert_protected_tx(
    result: TraceResult,
    protected_tables: frozenset[str] = AUTH_PROTECTED_TABLES,
    *,
    success: bool = True,
) -> None:
    transactions = {tx.number: tx for tx in result.transactions}
    rolled_back = {f.transaction for f in result.facts if f.kind == FactKind.ROLLBACK}

    def survives(fact: TraceFact) -> bool:
        number = fact.transaction
        while number is not None:
            if number in rolled_back:
                return False
            number = transactions[number].parent
        return True

    commits = [
        f for f in result.facts if f.kind == FactKind.COMMIT and f.transaction == f.root
    ]
    writes = [f for f in result.facts if f.kind == FactKind.SQL_WRITE and survives(f)]
    assert all(f.root is not None for f in writes), "unattributed SQL write"
    if success:
        assert len(commits) == 1, "success requires exactly one root commit"
        protected = [f for f in writes if f.table in protected_tables]
        assert protected, "no protected SQL write exercised"
        for fact in protected:
            assert any(
                a.root == fact.root
                and a.table == "audit_events"
                and a.evidence_insert
                and not any(
                    other.root == a.root
                    and other.table == "audit_events"
                    and other.operation in {"update", "delete"}
                    for other in writes
                )
                for a in writes
            ), "protected write lacks same-root canonical audit INSERT"
            assert any(c.root == fact.root for c in commits), (
                "uncommitted protected root"
            )
    else:
        assert not commits, "failed owner committed partial records"
        assert not writes, "failed owner left writes without rollback"
    protected_roots = {
        f.root
        for f in result.facts
        if f.kind == FactKind.SQL_WRITE and f.table in protected_tables
    }
    for effect in (f for f in result.facts if f.kind == FactKind.EXTERNAL):
        assert not protected_roots.intersection(effect.active_roots), (
            "external effect inside protected transaction"
        )
        intent_commits = [
            c
            for c in commits
            if c.root == effect.intent_root and c.order < effect.order
        ]
        assert intent_commits, "external effect before intent commit"
        assert any(
            write.root == effect.intent_root
            and write.table in EXTERNAL_INTENT_TABLES
            and write.evidence_insert
            and not any(
                other.root == write.root
                and other.table == write.table
                and other.operation in {"update", "delete"}
                for other in writes
            )
            and write.order < commit.order
            for write in writes
            for commit in intent_commits
        ), "external effect lacks committed transactional intent INSERT"
