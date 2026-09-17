"""Readable text and metadata extraction for captured web pages and PDFs."""

from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit

from pypdf import PdfReader
from trafilatura import bare_extraction
from trafilatura.settings import Document

_BOILERPLATE_XPATH = (
    "//nav | //footer | //aside | "
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' post-card ')] | "
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' share ')] | "
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' sharing ')] | "
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' sharedaddy ')] | "
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' social-share ')] | "
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' share-buttons ')]"
)


class ExtractionError(ValueError):
    """Raised when captured content has no extractable readable content."""


class NoTextLayerError(ExtractionError):
    """Raised when a PDF parses cleanly but carries no extractable text."""


@dataclass(frozen=True)
class ExtractedPage:
    """Readable content and descriptive metadata from one web page."""

    text: str
    title: str | None
    author: str | None
    site: str | None


def extract_web_page(content: bytes, *, url: str) -> ExtractedPage:
    """Extract a page's main text and metadata, excluding page boilerplate."""
    try:
        document = bare_extraction(
            content,
            url=url,
            favor_precision=True,
            include_comments=False,
            prune_xpath=_BOILERPLATE_XPATH,
            with_metadata=True,
        )
    except Exception as error:
        raise ExtractionError(f"web extraction failed: {error}") from error

    if not isinstance(document, Document) or not document.text:
        raise ExtractionError("web page contains no readable text")

    text = document.text.strip()
    if not text:
        raise ExtractionError("web page contains no readable text")

    site = document.sitename
    if site in {document.hostname, urlsplit(url).netloc}:
        site = None

    return ExtractedPage(
        text=text,
        title=document.title or None,
        author=document.author or None,
        site=site or None,
    )


@dataclass(frozen=True)
class ExtractedPdf:
    """Readable text and descriptive metadata from one PDF document."""

    text: str
    title: str | None
    author: str | None


def extract_pdf(content: bytes) -> ExtractedPdf:
    """Extract a PDF's text and metadata, page by page.

    Raises ``NoTextLayerError`` for a PDF that parses cleanly but has no
    extractable text (for example, a scanned or image-only PDF), so callers
    can treat that case as an expected, non-erroring outcome distinct from a
    malformed file.
    """
    try:
        reader = PdfReader(BytesIO(content))
        page_texts = [page.extract_text() for page in reader.pages]
        metadata = reader.metadata
    except Exception as error:
        raise ExtractionError(f"pdf extraction failed: {error}") from error

    text = "\n\n".join(
        page_text.strip() for page_text in page_texts if page_text.strip()
    )
    if not text:
        raise NoTextLayerError("pdf contains no extractable text")

    title = metadata.title if metadata is not None else None
    author = metadata.author if metadata is not None else None
    return ExtractedPdf(text=text, title=title or None, author=author or None)
