from typing import Any


class _BaseCursor:
    def execute(self, query: str, args: Any = ...) -> int: ...
    def fetchmany(self, size: int = ...) -> list[dict[str, Any]]: ...
    def __enter__(self) -> _BaseCursor: ...
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...


class cursors:
    class DictCursor(_BaseCursor): ...
    class SSDictCursor(_BaseCursor): ...


class Connection:
    def cursor(self) -> _BaseCursor: ...
    def close(self) -> None: ...


def connect(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    cursorclass: type[_BaseCursor],
    charset: str,
) -> Connection: ...
