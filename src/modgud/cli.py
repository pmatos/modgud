"""Command-line interface for modgud."""

import argparse
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from modgud.blobs import BlobStore
from modgud.database import connect
from modgud.formats import detect_format
from modgud.urls import canonicalize_url


def _default_data_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home is not None:
        return Path(data_home) / "modgud"
    return Path.home() / ".local" / "share" / "modgud"


def _fetch(url: str) -> tuple[bytes, str | None]:
    request = Request(url, headers={"User-Agent": "modgud/0.1"})
    with urlopen(request) as response:
        return response.read(), response.headers.get("Content-Type")


def _record_capture(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    url: str,
    canonical_url: str,
) -> None:
    payload = json.dumps(
        {
            "canonical_url": canonical_url,
            "origin": "manual",
            "url": url,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO events (item_id, type, payload) VALUES (?, 'captured', ?)",
        (item_id, payload),
    )


def _add(data_dir: Path, url: str) -> None:
    canonical_url = canonicalize_url(url)
    database = data_dir / "modgud.sqlite3"
    with connect(database) as connection:
        existing = connection.execute(
            "SELECT id FROM items WHERE canonical_url = ?",
            (canonical_url,),
        ).fetchone()
        if existing is not None:
            item_id = int(existing[0])
            _record_capture(
                connection,
                item_id=item_id,
                url=url,
                canonical_url=canonical_url,
            )
            print(f"Existing item {item_id}: {canonical_url}")
            return

    content, content_type = _fetch(url)
    content_hash = BlobStore(data_dir / "blobs").put(content)
    item_format = detect_format(
        canonical_url,
        content_type=content_type,
        content=content,
    )
    source = urlsplit(canonical_url).hostname or canonical_url

    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT id, canonical_url
            FROM items
            WHERE canonical_url = ? OR content_hash = ?
            ORDER BY id
            LIMIT 1
            """,
            (canonical_url, content_hash),
        ).fetchone()
        if existing is not None:
            item_id = int(existing[0])
            existing_url = str(existing[1])
            _record_capture(
                connection,
                item_id=item_id,
                url=url,
                canonical_url=canonical_url,
            )
            print(f"Existing item {item_id}: {existing_url}")
            return

        cursor = connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES (?, ?, ?, 'captured', ?)
            """,
            (canonical_url, content_hash, item_format, source),
        )
        inserted_item_id = cursor.lastrowid
        if inserted_item_id is None:
            raise RuntimeError("SQLite did not return an item id")
        _record_capture(
            connection,
            item_id=inserted_item_id,
            url=url,
            canonical_url=canonical_url,
        )

    print(f"Added item {inserted_item_id}: {canonical_url}")


def _list(data_dir: Path) -> None:
    with connect(data_dir / "modgud.sqlite3") as connection:
        items = connection.execute(
            """
            SELECT items.id, items.format, items.source, max(events.created_at)
            FROM items
            JOIN events ON events.item_id = items.id
            WHERE events.type = 'captured'
            GROUP BY items.id
            ORDER BY items.id
            """
        ).fetchall()

    print(f"{'id':<4} {'format':<10} {'source':<24} captured-at")
    for item_id, item_format, source, captured_at in items:
        print(f"{item_id:<4} {item_format:<10} {source:<24} {captured_at}")


def main() -> None:
    """Run the modgud command-line interface."""
    parser = argparse.ArgumentParser(
        prog="modgud",
        description="Triage personal content.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_default_data_dir(),
        help="directory for the database and raw content",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add", help="capture a URL")
    add_parser.add_argument("url")
    subparsers.add_parser("list", help="list captured items")

    arguments = parser.parse_args()
    data_dir: Path = arguments.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if arguments.command == "add":
        _add(data_dir, arguments.url)
    else:
        _list(data_dir)
