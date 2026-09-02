"""SQLite connection and schema migration support."""

import sqlite3
from pathlib import Path

_MIGRATIONS = (
    Path(__file__).with_name("migrations") / "001_initial.sql",
    Path(__file__).with_name("migrations") / "002_extracted_text_blob.sql",
    Path(__file__).with_name("migrations") / "003_web_metadata.sql",
    Path(__file__).with_name("migrations") / "004_time_to_value.sql",
    Path(__file__).with_name("migrations") / "005_youtube_metadata.sql",
    Path(__file__).with_name("migrations") / "006_tier_1_summaries.sql",
    Path(__file__).with_name("migrations") / "007_postmark_inbound.sql",
    Path(__file__).with_name("migrations") / "008_inbound_extraction.sql",
    Path(__file__).with_name("migrations") / "009_digest_schedule.sql",
)
_SCHEMA_VERSION = len(_MIGRATIONS)


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

    for migration in _MIGRATIONS[current_version:]:
        connection.executescript(migration.read_text(encoding="utf-8"))
