"""Readable text and metadata extraction for captured web pages and PDFs."""

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol
from urllib.parse import urlsplit

from pypdf import PdfReader
from trafilatura import bare_extraction
from trafilatura.settings import Document

from modgud.formats import ItemFormat

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


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Readable text and normalized metadata from one document."""

    text: str
    title: str | None
    author: str | None
    site: str | None


class _DocumentFormatAdapter(Protocol):
    def __call__(
        self,
        content: bytes,
        *,
        url: str,
    ) -> ExtractedDocument: ...


def _extract_web_document(content: bytes, *, url: str) -> ExtractedDocument:
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

    return ExtractedDocument(
        text=text,
        title=document.title or None,
        author=document.author or None,
        site=site or None,
    )


def _extract_pdf_document(content: bytes, *, url: str) -> ExtractedDocument:
    """Extract a PDF's text and metadata, page by page."""
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
    return ExtractedDocument(
        text=text,
        title=title or None,
        author=author or None,
        site=None,
    )


_DOCUMENT_FORMAT_ADAPTERS: dict[ItemFormat, _DocumentFormatAdapter] = {
    ItemFormat.WEB: _extract_web_document,
    ItemFormat.PDF: _extract_pdf_document,
}


def extract_document(
    content: bytes,
    *,
    item_format: ItemFormat,
    url: str,
) -> ExtractedDocument:
    """Extract readable text and metadata using the document's format."""
    try:
        adapter = _DOCUMENT_FORMAT_ADAPTERS[item_format]
    except KeyError:
        raise ValueError(f"unsupported document format: {item_format}") from None
    return adapter(content, url=url)
