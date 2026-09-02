"""SQLite connection and schema migration support."""

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Literal

_MIGRATION = Path(__file__).with_name("migrations") / "001_initial.sql"
_SCHEMA_VERSION = 1


class _ClosingConnection(sqlite3.Connection):
    """A connection that closes itself when used as a context manager.

    ``sqlite3.Connection.__exit__`` only commits or rolls back the open
    transaction; it never closes the connection. That leaves ``with
    connect(...) as connection:`` leaking a connection on every use.
    """

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> Literal[False]:
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a database and bring its schema up to date."""
    connection = sqlite3.connect(database, factory=_ClosingConnection)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return connection


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply outstanding schema migrations to an open database."""
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version >= _SCHEMA_VERSION:
        return

    connection.executescript(_MIGRATION.read_text(encoding="utf-8"))
