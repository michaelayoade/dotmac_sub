"""Server-owned full PostgreSQL outer transaction identity, never an authority input."""

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine, UserDefinedType


class _PostgresXid8(UserDefinedType[str]):
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "xid8"


class OuterTransactionId(TypeDecorator[str]):
    """Keep xid8 precise as decimal text; SQLite provides storage-only parity."""

    impl = String(20)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[str]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(_PostgresXid8())
        return dialect.type_descriptor(String(20))

    def process_result_value(self, value: object, dialect: Dialect) -> str | None:
        return None if value is None else str(value)
