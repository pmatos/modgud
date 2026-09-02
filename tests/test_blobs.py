"""Behavioral tests for content-addressed blob storage."""

from pathlib import Path

import pytest

from modgud.blobs import BlobStore
from modgud.database import connect


def test_stored_blob_is_retrievable_by_sha256_hash(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)

    blob_hash = store.put(b"hello world")

    assert blob_hash == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )
    assert store.get(blob_hash) == b"hello world"


def test_storing_the_same_bytes_twice_creates_one_inspectable_blob(
    tmp_path: Path,
) -> None:
    store = BlobStore(tmp_path)
    expected_hash = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"

    first_hash = store.put(b"hello world")
    second_hash = store.put(b"hello world")
    stored_files = [path for path in tmp_path.rglob("*") if path.is_file()]

    assert (first_hash, second_hash, stored_files) == (
        expected_hash,
        expected_hash,
        [tmp_path / "sha256" / "b9" / expected_hash],
    )


def test_storing_existing_content_does_not_rewrite_the_blob(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    blob_hash = store.put(b"immutable")
    blob_path = tmp_path / "sha256" / blob_hash[:2] / blob_hash
    blob_path.chmod(0o444)

    repeated_hash = store.put(b"immutable")

    assert (repeated_hash, blob_path.read_bytes()) == (blob_hash, b"immutable")


def test_item_row_references_raw_and_extracted_text_blobs(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "blobs")
    raw_hash = store.put(b"<article>Hello</article>")
    text_hash = store.put("Héllo".encode())

    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (
                canonical_url,
                content_hash,
                extracted_text_hash,
                format,
                state,
                source
            ) VALUES (?, ?, ?, 'web', 'extracted', 'example.com')
            """,
            ("https://example.com/article", raw_hash, text_hash),
        )
        stored_hashes = connection.execute(
            "SELECT content_hash, extracted_text_hash FROM items"
        ).fetchone()

    assert (stored_hashes, tuple(store.get(value) for value in stored_hashes)) == (
        (raw_hash, text_hash),
        (b"<article>Hello</article>", b"H\xc3\xa9llo"),
    )


def test_retrieval_rejects_values_that_are_not_sha256_hashes(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)

    with pytest.raises(ValueError, match="SHA-256"):
        store.get("../../not-a-hash")


def test_retrieval_rejects_content_that_does_not_match_its_hash(
    tmp_path: Path,
) -> None:
    store = BlobStore(tmp_path)
    blob_hash = store.put(b"hello world")
    blob_path = tmp_path / "sha256" / blob_hash[:2] / blob_hash
    blob_path.chmod(0o644)
    blob_path.write_bytes(b"corrupted")

    with pytest.raises(ValueError, match="does not match hash"):
        store.get(blob_hash)
