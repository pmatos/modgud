"""Conservative format detection for fetched items."""

from enum import StrEnum
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from zipfile import BadZipFile, ZipFile

_DECK_MEDIA_TYPES = frozenset(
    {
        "application/vnd.apple.keynote",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/x-iwork-keynote-sffkey",
    }
)
_OPEN_DOCUMENT_PRESENTATION_TYPE = b"application/vnd.oasis.opendocument.presentation"


class ItemFormat(StrEnum):
    """The format that selects an item's downstream processing path."""

    DECK = "deck"
    PAPER = "paper"
    PDF = "pdf"
    PODCAST = "podcast"
    UNKNOWN = "unknown"
    WEB = "web"
    YOUTUBE = "youtube"


def detect_format(
    url: str,
    *,
    content_type: str | None = None,
    content: bytes = b"",
) -> ItemFormat:
    """Return the format supported by the strongest available evidence."""
    url_format = _detect_from_url(url)
    if url_format is not ItemFormat.UNKNOWN:
        return url_format

    media_type = (content_type or "").partition(";")[0].strip().lower()
    content_type_format = _detect_from_content_type(media_type)
    if content_type_format is not ItemFormat.UNKNOWN:
        return content_type_format

    return _detect_from_magic_bytes(content)


def _detect_from_url(url: str) -> ItemFormat:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ItemFormat.UNKNOWN

    host = (parts.hostname or "").lower().rstrip(".")
    path_parts = [part for part in parts.path.split("/") if part]
    path = parts.path.rstrip("/")
    query = parse_qs(parts.query)

    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        return ItemFormat.YOUTUBE
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if path == "/watch" and query.get("v"):
            return ItemFormat.YOUTUBE
        if len(path_parts) >= 2 and path_parts[0] in {"embed", "live", "shorts"}:
            return ItemFormat.YOUTUBE
    if (
        host in {"youtube-nocookie.com", "www.youtube-nocookie.com"}
        and len(path_parts) >= 2
        and path_parts[0] == "embed"
    ):
        return ItemFormat.YOUTUBE

    if (
        host in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"}
        and len(path_parts) >= 2
        and path_parts[0] in {"abs", "pdf"}
    ):
        return ItemFormat.PAPER
    if (
        host in {"openreview.net", "www.openreview.net"}
        and path in {"/forum", "/pdf"}
        and query.get("id")
    ):
        return ItemFormat.PAPER

    path = path.lower()
    if path.endswith(".pdf"):
        return ItemFormat.PDF
    if path.endswith((".odp", ".ppt", ".pptx")):
        return ItemFormat.DECK
    if (
        host
        in {
            "slides.com",
            "speakerdeck.com",
            "www.slides.com",
            "www.slideshare.net",
            "www.speakerdeck.com",
        }
        and len(path_parts) >= 2
    ):
        return ItemFormat.DECK
    if (
        host == "docs.google.com"
        and len(path_parts) >= 3
        and path_parts[:2] == ["presentation", "d"]
    ):
        return ItemFormat.DECK

    if parts.scheme.lower() == "podcast" and len(path_parts) >= 2:
        return ItemFormat.PODCAST
    if (
        host == "open.spotify.com"
        and len(path_parts) >= 2
        and path_parts[0] == "episode"
    ):
        return ItemFormat.PODCAST
    if host == "podcasts.apple.com" and "podcast" in path_parts and query.get("i"):
        return ItemFormat.PODCAST

    return ItemFormat.UNKNOWN


def _detect_from_content_type(media_type: str) -> ItemFormat:
    if media_type in {"application/xhtml+xml", "text/html"}:
        return ItemFormat.WEB
    if media_type == "application/pdf":
        return ItemFormat.PDF
    if media_type in _DECK_MEDIA_TYPES:
        return ItemFormat.DECK
    return ItemFormat.UNKNOWN


def _detect_from_magic_bytes(content: bytes) -> ItemFormat:
    if content.startswith(b"%PDF-"):
        return ItemFormat.PDF
    if content.startswith(b"PK") and _is_presentation_archive(content):
        return ItemFormat.DECK
    return ItemFormat.UNKNOWN


def _is_presentation_archive(content: bytes) -> bool:
    try:
        with ZipFile(BytesIO(content)) as archive:
            try:
                archive.getinfo("ppt/presentation.xml")
            except KeyError:
                mimetype = archive.getinfo("mimetype")
                if mimetype.file_size != len(_OPEN_DOCUMENT_PRESENTATION_TYPE):
                    return False
                return archive.read(mimetype) == _OPEN_DOCUMENT_PRESENTATION_TYPE
            else:
                return True
    except (BadZipFile, KeyError, NotImplementedError, OSError, RuntimeError):
        return False
