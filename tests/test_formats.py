"""Behavioral tests for fetched-item format detection."""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from modgud.database import connect
from modgud.formats import ItemFormat, detect_format


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/AbC_123",
        "https://www.youtube.com/watch?v=AbC_123",
        "https://youtube.com/shorts/AbC_123",
        "https://www.youtube-nocookie.com/embed/AbC_123",
    ],
)
def test_youtube_video_urls_are_detected_without_response_metadata(url: str) -> None:
    assert detect_format(url) is ItemFormat.YOUTUBE


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/abs/2401.12345",
        "https://arxiv.org/pdf/2401.12345",
        "https://openreview.net/forum?id=paper123",
        "https://openreview.net/pdf?id=paper123",
    ],
)
def test_research_repository_urls_are_detected_as_papers(url: str) -> None:
    assert detect_format(url) is ItemFormat.PAPER


@pytest.mark.parametrize(
    "url",
    [
        "podcast:feed-hash/episode-guid",
        "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk",
        "https://podcasts.apple.com/us/podcast/example/id123456?i=987654",
    ],
)
def test_explicit_podcast_episode_urls_are_detected(url: str) -> None:
    assert detect_format(url) is ItemFormat.PODCAST


def test_pdf_file_urls_are_detected_without_response_metadata() -> None:
    assert (
        detect_format("https://example.com/reports/RESULTS.PDF?download=1")
        is ItemFormat.PDF
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://speakerdeck.com/example/a-talk",
        "https://slides.com/example/a-talk",
        "https://www.slideshare.net/example/a-talk",
        "https://docs.google.com/presentation/d/deck-id/edit",
        "https://example.com/talks/a-talk.pptx?download=1",
    ],
)
def test_presentation_urls_are_detected_as_decks(url: str) -> None:
    assert detect_format(url) is ItemFormat.DECK


@pytest.mark.parametrize(
    "content_type",
    ["Text/HTML; charset=UTF-8", "application/xhtml+xml"],
)
def test_html_content_types_detect_a_web_page(content_type: str) -> None:
    assert (
        detect_format(
            "https://example.com/an-article",
            content_type=content_type,
        )
        is ItemFormat.WEB
    )


def test_pdf_content_type_is_used_when_the_url_is_ambiguous() -> None:
    assert (
        detect_format(
            "https://example.com/download?id=42",
            content_type="application/pdf",
        )
        is ItemFormat.PDF
    )


@pytest.mark.parametrize(
    "content_type",
    [
        "application/vnd.apple.keynote",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.presentation",
        "application/x-iwork-keynote-sffkey",
    ],
)
def test_presentation_content_types_detect_decks(content_type: str) -> None:
    assert (
        detect_format(
            "https://example.com/download?id=42",
            content_type=content_type,
        )
        is ItemFormat.DECK
    )


def test_pdf_magic_bytes_are_used_after_an_uninformative_content_type() -> None:
    assert (
        detect_format(
            "https://example.com/download?id=42",
            content_type="application/octet-stream",
            content=b"%PDF-1.7\n% test document",
        )
        is ItemFormat.PDF
    )


def test_presentation_archive_structure_detects_a_deck() -> None:
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("ppt/presentation.xml", "<p:presentation />")

    assert (
        detect_format("https://example.com/download?id=42", content=content.getvalue())
        is ItemFormat.DECK
    )


def test_open_document_presentation_magic_bytes_detect_a_deck() -> None:
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.presentation",
        )

    assert (
        detect_format("https://example.com/download?id=42", content=content.getvalue())
        is ItemFormat.DECK
    )


@pytest.mark.parametrize(
    ("url", "content_type", "content"),
    [
        ("https://example.com/item", None, b""),
        ("https://[invalid", None, b""),
        ("https://www.youtube.com/", None, b""),
        ("https://doi.org/10.1234/example", None, b""),
        ("https://example.com/audio.mp3", "audio/mpeg", b"ID3audio"),
        ("https://example.com/video.mp4", "video/mp4", b"video"),
        ("https://example.com/item", "text/plain", b"<html>maybe markup</html>"),
    ],
)
def test_ambiguous_evidence_returns_unknown(
    url: str,
    content_type: str | None,
    content: bytes,
) -> None:
    assert (
        detect_format(url, content_type=content_type, content=content)
        is ItemFormat.UNKNOWN
    )


def test_evidence_precedence_is_url_then_content_type_then_magic_bytes() -> None:
    assert (
        detect_format(
            "https://youtu.be/AbC_123",
            content_type="text/html",
            content=b"%PDF-1.7",
        ),
        detect_format(
            "https://example.com/download?id=42",
            content_type="text/html",
            content=b"%PDF-1.7",
        ),
    ) == (ItemFormat.YOUTUBE, ItemFormat.WEB)


def test_generic_zip_archive_is_not_guessed_to_be_a_deck() -> None:
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("document.txt", "not a presentation")

    assert (
        detect_format("https://example.com/download?id=42", content=content.getvalue())
        is ItemFormat.UNKNOWN
    )


def test_unknown_format_is_storable_without_conversion(tmp_path: Path) -> None:
    item_format = detect_format("https://example.com/unrecognized")

    with connect(tmp_path / "modgud.sqlite3") as connection:
        connection.execute(
            """
            INSERT INTO items (canonical_url, content_hash, format, state, source)
            VALUES (?, ?, ?, 'unsummarizable', 'example.com')
            """,
            ("https://example.com/unrecognized", "unknown-content", item_format),
        )
        stored_format = connection.execute("SELECT format FROM items").fetchone()[0]

    assert (item_format, stored_format) == (ItemFormat.UNKNOWN, "unknown")
