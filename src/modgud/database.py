"""SQLite connection and schema migration support."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
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
    Path(__file__).with_name("migrations") / "010_span_maps.sql",
    Path(__file__).with_name("migrations") / "011_tier_2_summaries.sql",
    Path(__file__).with_name("migrations") / "012_item_page_url.sql",
)
_SCHEMA_VERSION = len(_MIGRATIONS)


def connect(database: str | Path) -> sqlite3.Connection:
    """Open a migrated database; the caller owns closing the connection.

    Its native context commits or rolls back without closing. Use
    ``transaction`` for a scope that owns both lifetimes.
    """
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        apply_migrations(connection)
    except Exception:
        connection.close()
        raise
    return connection


@contextmanager
def transaction(database: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a migrated connection, commit or roll back, then close.

    No transaction is started on entry; callers retain explicit locking and
    SQLite's implicit transaction behavior. The connection must stay inside
    this scope. Transaction completion runs before closing, even on failure.
    """
    with closing(connect(database)) as connection, connection:
        yield connection


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Apply outstanding schema migrations to an open database."""
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version >= _SCHEMA_VERSION:
        return

    for migration in _MIGRATIONS[current_version:]:
        connection.executescript(migration.read_text(encoding="utf-8"))
