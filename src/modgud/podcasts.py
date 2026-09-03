"""Podcast feed parsing and episode identities."""

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from math import isfinite
from typing import cast
from urllib.parse import urljoin
from xml.etree import ElementTree

from modgud.urls import canonicalize_url

_ITUNES_NAMESPACE = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_PODCAST_NAMESPACES = (
    "https://podcastindex.org/namespace/1.0",
    "https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/1.0.md",
)


class PodcastFeedError(ValueError):
    """Raised when fetched content is not a usable podcast feed."""


@dataclass(frozen=True)
class PodcastTranscript:
    """One creator-supplied transcript linked by a podcast episode."""

    url: str
    media_type: str
    language: str | None
    rel: str | None


@dataclass(frozen=True)
class PodcastEpisodeResources:
    """Transcript and audio links retained in a captured episode manifest."""

    transcripts: tuple[PodcastTranscript, ...]
    audio_url: str | None


@dataclass(frozen=True)
class PodcastEpisode:
    """Metadata needed to capture one episode from a podcast feed."""

    canonical_url: str
    feed_url: str
    guid: str
    title: str | None
    author: str | None
    podcast_title: str | None
    duration_seconds: float | None
    transcripts: tuple[PodcastTranscript, ...]
    audio_url: str | None
    raw_content: bytes


@dataclass(frozen=True)
class _FeedEntry:
    position: int
    guid: str
    page_url: str | None
    title: str | None
    author: str | None
    duration_seconds: float | None
    published_at: datetime | None
    raw_element: ElementTree.Element


def discover_podcast_feed(content: bytes, *, page_url: str) -> str | None:
    """Return the first RSS or Atom autodiscovery link in an HTML page."""
    parser = _FeedLinkParser()
    try:
        parser.feed(content.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if parser.feed_url is None:
        return None
    return canonicalize_url(urljoin(page_url, parser.feed_url))


def parse_podcast_episode_resources(content: bytes) -> PodcastEpisodeResources:
    """Recover transcript and audio links from a captured episode manifest."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise PodcastFeedError("episode manifest is not XML") from error
    if root.tag != "podcast-episode":
        raise PodcastFeedError("content is not a podcast episode manifest")
    feed_url = root.get("feed_url")
    entries = list(root)
    if not feed_url or len(entries) != 1:
        raise PodcastFeedError("podcast episode manifest is incomplete")
    entry = entries[0]
    return PodcastEpisodeResources(
        transcripts=_podcast_transcripts(entry, feed_url=feed_url),
        audio_url=_episode_audio_url(entry, feed_url=feed_url),
    )


def parse_podcast_feed(
    content: bytes,
    *,
    feed_url: str,
    episode_url: str | None = None,
) -> PodcastEpisode:
    """Parse an RSS feed and return its latest identified episode."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise PodcastFeedError("response is not an XML feed") from error

    channel = root.find("channel")
    if channel is not None:
        podcast_title = _text(channel.find("title"))
        entries = _rss_entries(channel)
    elif root.tag == f"{{{_ATOM_NAMESPACE}}}feed":
        podcast_title = _text(root.find(f"{{{_ATOM_NAMESPACE}}}title"))
        entries = _atom_entries(root)
    else:
        raise PodcastFeedError("XML document is not an RSS or Atom feed")
    if not entries:
        raise PodcastFeedError("feed has no identified episodes")

    canonical_feed_url = canonicalize_url(feed_url)
    if episode_url is None:
        selected = max(
            entries,
            key=lambda entry: (
                entry.published_at is not None,
                entry.published_at or datetime.min.replace(tzinfo=UTC),
                -entry.position,
            ),
        )
    else:
        canonical_episode_url = canonicalize_url(episode_url)
        matching = [
            entry
            for entry in entries
            if entry.page_url is not None
            and canonicalize_url(urljoin(canonical_feed_url, entry.page_url))
            == canonical_episode_url
        ]
        if not matching:
            raise PodcastFeedError("episode page is not present in its feed")
        selected = matching[0]

    feed_hash = sha256(canonical_feed_url.encode("utf-8")).hexdigest()
    canonical_url = f"podcast:{feed_hash}/{selected.guid}"
    transcripts = _podcast_transcripts(
        selected.raw_element,
        feed_url=canonical_feed_url,
    )
    audio_url = _episode_audio_url(
        selected.raw_element,
        feed_url=canonical_feed_url,
    )
    return PodcastEpisode(
        canonical_url=canonical_url,
        feed_url=canonical_feed_url,
        guid=selected.guid,
        title=selected.title,
        author=selected.author,
        podcast_title=podcast_title,
        duration_seconds=selected.duration_seconds,
        transcripts=transcripts,
        audio_url=audio_url,
        raw_content=_episode_manifest(
            selected,
            canonical_url=canonical_url,
            feed_url=canonical_feed_url,
        ),
    )


class _FeedLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.feed_url: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "link" or self.feed_url is not None:
            return
        fields = {name.lower(): value for name, value in attrs}
        relation = (fields.get("rel") or "").lower().split()
        media_type = (fields.get("type") or "").lower().partition(";")[0]
        href = fields.get("href")
        if (
            "alternate" in relation
            and media_type in {"application/atom+xml", "application/rss+xml"}
            and href
        ):
            self.feed_url = href


def _episode_manifest(
    episode: _FeedEntry,
    *,
    canonical_url: str,
    feed_url: str,
) -> bytes:
    root = ElementTree.Element(
        "podcast-episode",
        {
            "canonical_url": canonical_url,
            "feed_url": feed_url,
            "guid": episode.guid,
        },
    )
    root.append(episode.raw_element)
    return cast(bytes, ElementTree.tostring(root, encoding="utf-8"))


def _rss_entries(channel: ElementTree.Element) -> list[_FeedEntry]:
    entries = []
    for position, item in enumerate(channel.findall("item")):
        guid = _text(item.find("guid"))
        if guid is None:
            continue
        entries.append(
            _FeedEntry(
                position=position,
                guid=guid,
                page_url=_text(item.find("link")),
                title=_text(item.find("title")),
                author=_text(item.find(f"{{{_ITUNES_NAMESPACE}}}author"))
                or _text(item.find("author")),
                duration_seconds=_duration_seconds(
                    _text(item.find(f"{{{_ITUNES_NAMESPACE}}}duration"))
                ),
                published_at=_rss_published_at(item),
                raw_element=item,
            )
        )
    return entries


def _atom_entries(feed: ElementTree.Element) -> list[_FeedEntry]:
    entries = []
    for position, entry in enumerate(feed.findall(f"{{{_ATOM_NAMESPACE}}}entry")):
        guid = _text(entry.find(f"{{{_ATOM_NAMESPACE}}}id"))
        if guid is None:
            continue
        author = entry.find(f"{{{_ATOM_NAMESPACE}}}author")
        entries.append(
            _FeedEntry(
                position=position,
                guid=guid,
                page_url=_atom_page_url(entry),
                title=_text(entry.find(f"{{{_ATOM_NAMESPACE}}}title")),
                author=_text(entry.find(f"{{{_ITUNES_NAMESPACE}}}author"))
                or _text(
                    author.find(f"{{{_ATOM_NAMESPACE}}}name")
                    if author is not None
                    else None
                ),
                duration_seconds=_duration_seconds(
                    _text(entry.find(f"{{{_ITUNES_NAMESPACE}}}duration"))
                ),
                published_at=_atom_published_at(entry),
                raw_element=entry,
            )
        )
    return entries


def _atom_page_url(entry: ElementTree.Element) -> str | None:
    for link in entry.findall(f"{{{_ATOM_NAMESPACE}}}link"):
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href")
    return None


def _podcast_transcripts(
    entry: ElementTree.Element,
    *,
    feed_url: str,
) -> tuple[PodcastTranscript, ...]:
    transcripts = []
    for namespace in _PODCAST_NAMESPACES:
        for transcript in entry.findall(f"{{{namespace}}}transcript"):
            url = transcript.get("url")
            media_type = transcript.get("type")
            if not url or not media_type:
                continue
            transcripts.append(
                PodcastTranscript(
                    url=urljoin(feed_url, url),
                    media_type=media_type.partition(";")[0].strip().lower(),
                    language=transcript.get("language"),
                    rel=transcript.get("rel"),
                )
            )
    return tuple(transcripts)


def _episode_audio_url(
    entry: ElementTree.Element,
    *,
    feed_url: str,
) -> str | None:
    if entry.tag == f"{{{_ATOM_NAMESPACE}}}entry":
        candidates = entry.findall(f"{{{_ATOM_NAMESPACE}}}link")
        for link in candidates:
            media_type = (link.get("type") or "").lower()
            href = link.get("href")
            if (
                link.get("rel") == "enclosure"
                and media_type.startswith("audio/")
                and href
            ):
                return urljoin(feed_url, href)
        return None

    for enclosure in entry.findall("enclosure"):
        media_type = (enclosure.get("type") or "").lower()
        url = enclosure.get("url")
        if media_type.startswith("audio/") and url:
            return urljoin(feed_url, url)
    return None


def _rss_published_at(item: ElementTree.Element) -> datetime | None:
    published = _text(item.find("pubDate"))
    if published is None:
        return None
    try:
        parsed = parsedate_to_datetime(published)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=UTC)


def _atom_published_at(entry: ElementTree.Element) -> datetime | None:
    published = _text(entry.find(f"{{{_ATOM_NAMESPACE}}}published")) or _text(
        entry.find(f"{{{_ATOM_NAMESPACE}}}updated")
    )
    if published is None:
        return None
    try:
        parsed = datetime.fromisoformat(published)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=UTC)


def _duration_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    fields = value.split(":")
    try:
        if len(fields) == 1:
            duration = float(fields[0])
        elif len(fields) == 2:
            minutes, seconds = fields
            duration = int(minutes) * 60 + float(seconds)
        elif len(fields) == 3:
            hours, minutes, seconds = fields
            duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        else:
            return None
    except ValueError:
        return None
    if duration < 0 or not isfinite(duration):
        return None
    return duration


def _text(element: ElementTree.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None
