"""SQLite connection and schema migration support."""

import sqlite3
from pathlib import Path

_MIGRATION = Path(__file__).with_name("migrations") / "001_initial.sql"
_SCHEMA_VERSION = 1


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a database and bring its schema up to date."""
    connection = sqlite3.connect(database)
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
