"""Select and render items for the next digest."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import urlencode, urlsplit, urlunsplit

from modgud.config import SecretValue
from modgud.formats import ItemFormat
from modgud.label_tokens import create_label_token
from modgud.span_maps import SpanMap, get_span_map
from modgud.summaries import Tier1Summary

_INLINE_ITEM_LIMIT = 10
_CAPTURE_ONLY = "Capture only — no summary is available."


@dataclass(frozen=True, slots=True)
class DigestItem:
    """An item selected for inclusion in the next digest."""

    id: int
    canonical_url: str
    format: ItemFormat
    state: str
    source: str
    title: str | None
    author: str | None
    time_to_value_seconds: int | None
    summary: Tier1Summary | None
    span_map: SpanMap | None = None


@dataclass(frozen=True, slots=True)
class RenderedDigest:
    """The alternative bodies of one self-contained email digest."""

    html: str
    text: str


def _item_title(item: DigestItem) -> str:
    return item.title or item.source


def _item_metadata(item: DigestItem) -> str:
    parts = [item.source]
    if item.author is not None:
        parts.append(item.author)
    if item.time_to_value_seconds is not None:
        minutes = (item.time_to_value_seconds + 59) // 60
        parts.append(f"{minutes} min")
    return " · ".join(parts)


def _format_timestamp(milliseconds: int) -> str:
    total_seconds = milliseconds // 1_000
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _span_link(item: DigestItem, start_ms: int) -> str | None:
    if item.format is not ItemFormat.YOUTUBE:
        return None
    parts = urlsplit(item.canonical_url)
    timestamp = f"t={start_ms // 1_000}s"
    query = f"{parts.query}&{timestamp}" if parts.query else timestamp
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _label_link(
    item_id: int,
    label: str,
    *,
    base_url: str,
    signing_secret: SecretValue,
    expires_at: datetime,
) -> str:
    token = create_label_token(
        item_id,
        label,
        signing_secret=signing_secret,
        expires_at=expires_at,
    )
    return (
        f"{base_url.rstrip('/')}/items/{item_id}/labels/{label}?"
        f"{urlencode({'token': token})}"
    )


def render_digest(
    items: Sequence[DigestItem],
    *,
    label_base_url: str,
    label_signing_secret: SecretValue,
    label_token_lifetime: timedelta,
    now: datetime | None = None,
) -> RenderedDigest:
    """Render selected items as complete HTML and plain-text email bodies."""
    expires_at = (now or datetime.now(UTC)) + label_token_lifetime
    text_parts = ["modgud digest", "=============", ""]
    html_parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        "<title>modgud digest</title></head><body>",
        "<h1>modgud digest</h1>",
    ]
    for position, item in enumerate(items, start=1):
        title = _item_title(item)
        metadata = _item_metadata(item)
        worth_it_link = _label_link(
            item.id,
            "worth-it",
            base_url=label_base_url,
            signing_secret=label_signing_secret,
            expires_at=expires_at,
        )
        not_worth_it_link = _label_link(
            item.id,
            "not-worth-it",
            base_url=label_base_url,
            signing_secret=label_signing_secret,
            expires_at=expires_at,
        )
        if position > _INLINE_ITEM_LIMIT:
            if position == _INLINE_ITEM_LIMIT + 1:
                text_parts.extend(["More items", "----------", ""])
                html_parts.extend(["<section><h2>More items</h2>", '<ol start="11">'])
            description = (
                item.summary.one_liner if item.summary is not None else _CAPTURE_ONLY
            )
            text_parts.append(
                f"{position}. {title} — {description} — {item.source} — "
                f"{item.canonical_url} — 👍 Worth it: {worth_it_link} — "
                f"👎 Not worth it: {not_worth_it_link}"
            )
            html_parts.append(
                f'<li><a href="{escape(item.canonical_url, quote=True)}">'
                f"{escape(title)}</a> — {escape(description)} — "
                f"{escape(item.source)}<br>"
                f'<a href="{escape(worth_it_link, quote=True)}">👍 Worth it</a> · '
                f'<a href="{escape(not_worth_it_link, quote=True)}">'
                "👎 Not worth it</a></li>"
            )
            continue
        text_parts.extend(
            [
                f"{position}. {title}",
                f"   {metadata}",
                f"   {item.canonical_url}",
                f"   👍 Worth it: {worth_it_link}",
                f"   👎 Not worth it: {not_worth_it_link}",
                "",
            ]
        )
        html_parts.extend(
            [
                "<article>",
                (
                    f'<h2>{position}. <a href="{escape(item.canonical_url, quote=True)}">'
                    f"{escape(title)}</a></h2>"
                ),
                f"<p>{escape(metadata)}</p>",
                (
                    f'<p><a href="{escape(worth_it_link, quote=True)}">'
                    "👍 Worth it</a> · "
                    f'<a href="{escape(not_worth_it_link, quote=True)}">'
                    "👎 Not worth it</a></p>"
                ),
            ]
        )
        if item.summary is None:
            text_parts.extend([f"   {_CAPTURE_ONLY}", ""])
            html_parts.extend([f"<p>{_CAPTURE_ONLY}</p>", "</article>"])
            continue
        text_parts.extend(
            [
                f"   {item.summary.one_liner}",
                "",
                "   Claims:",
                *(f"   - {claim}" for claim in item.summary.claims),
                "",
            ]
        )
        html_parts.extend(
            [
                f"<p>{escape(item.summary.one_liner)}</p>",
                "<h3>Claims</h3><ul>",
                *(f"<li>{escape(claim)}</li>" for claim in item.summary.claims),
            ]
        )
        if item.span_map is not None and item.span_map.spans:
            text_parts.append("   Span map:")
            html_parts.extend(["</ul>", "<h3>Span map</h3><ul>"])
            for span in item.span_map.spans:
                timestamp = (
                    f"{_format_timestamp(span.start_ms)}–"
                    f"{_format_timestamp(span.end_ms)}"
                )
                link = _span_link(item, span.start_ms)
                text_line = f"   - {timestamp} — {span.description}"
                if link is not None:
                    text_line = f"{text_line} — {link}"
                    html_timestamp = (
                        f'<a href="{escape(link, quote=True)}">{timestamp}</a>'
                    )
                else:
                    html_timestamp = timestamp
                text_parts.append(text_line)
                html_parts.append(
                    f"<li>{html_timestamp} — {escape(span.description)}</li>"
                )
            text_parts.append("")
            html_parts.append("</ul></article>")
        else:
            html_parts.append("</ul></article>")
    if len(items) > _INLINE_ITEM_LIMIT:
        html_parts.append("</ol></section>")
    html_parts.append("</body></html>")
    return RenderedDigest(html="".join(html_parts), text="\n".join(text_parts))


def _stored_summary(one_liner: object, claims_json: object) -> Tier1Summary | None:
    if one_liner is None and claims_json is None:
        return None
    if one_liner is None or claims_json is None:
        raise ValueError("stored tier-1 summary must have one-liner and claims")
    claims = json.loads(str(claims_json))
    if not isinstance(claims, list) or any(
        not isinstance(claim, str) for claim in claims
    ):
        raise ValueError("stored tier-1 claims must be an array of strings")
    return Tier1Summary(
        one_liner=str(one_liner),
        claims=tuple(claims),
    )


def select_digest_items(connection: sqlite3.Connection) -> tuple[DigestItem, ...]:
    """Return digest-visible items captured after the last successful send."""
    rows = connection.execute(
        """
        WITH last_success AS (
            SELECT coalesce(max(id), 0) AS event_id
            FROM events
            WHERE type = 'digest_sent'
        ),
        qualifying_captures AS (
            SELECT captures.item_id,
                   min(captures.id) AS first_capture_event_id
            FROM events AS captures
            CROSS JOIN last_success
            WHERE captures.type = 'captured'
              AND captures.id > last_success.event_id
            GROUP BY captures.item_id
        )
        SELECT items.id,
               items.canonical_url,
               items.format,
               items.state,
               items.source,
               items.title,
               items.author,
               items.time_to_value_seconds,
               tier_1_summaries.one_liner,
               tier_1_summaries.claims
        FROM qualifying_captures
        JOIN items ON items.id = qualifying_captures.item_id
        LEFT JOIN tier_1_summaries ON tier_1_summaries.item_id = items.id
        WHERE items.state IN ('summarized', 'unsummarizable', 'failed')
        ORDER BY items.time_to_value_seconds IS NULL,
                 items.time_to_value_seconds,
                 qualifying_captures.first_capture_event_id,
                 items.id
        """
    ).fetchall()
    return tuple(
        DigestItem(
            id=int(row[0]),
            canonical_url=str(row[1]),
            format=ItemFormat(row[2]),
            state=str(row[3]),
            source=str(row[4]),
            title=str(row[5]) if row[5] is not None else None,
            author=str(row[6]) if row[6] is not None else None,
            time_to_value_seconds=int(row[7]) if row[7] is not None else None,
            summary=_stored_summary(row[8], row[9]),
            span_map=get_span_map(connection, int(row[0])),
        )
        for row in rows
    )
