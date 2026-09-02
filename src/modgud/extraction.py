"""Readable text and metadata extraction for captured web pages."""

from dataclasses import dataclass
from urllib.parse import urlsplit

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
    """Raised when a captured page has no extractable readable content."""


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
