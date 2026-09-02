"""Per-item time-to-value estimates."""

import sqlite3
from math import ceil

from modgud.formats import ItemFormat

_READING_WORDS_PER_MINUTE = 200
_AUDIO_VIDEO_FORMATS = frozenset({ItemFormat.PODCAST, ItemFormat.YOUTUBE})


def time_to_value_sort_key(seconds: int | None) -> tuple[bool, int]:
    """Order known estimates from shortest to longest, with unknown last."""
    return seconds is None, seconds or 0


def estimate_time_to_value_seconds(
    item_format: ItemFormat,
    *,
    extracted_text: str | None = None,
    duration_seconds: float | None = None,
) -> int | None:
    """Estimate engagement time from the metadata appropriate to the format."""
    if item_format in _AUDIO_VIDEO_FORMATS:
        if duration_seconds is None:
            return None
        return ceil(duration_seconds)

    if not extracted_text:
        return None
    word_count = len(extracted_text.split())
    if word_count == 0:
        return None
    return ceil(word_count * 60 / _READING_WORDS_PER_MINUTE)


def recompute_time_to_value(
    connection: sqlite3.Connection,
    *,
    item_id: int,
    extracted_text: str | None = None,
) -> int | None:
    """Recompute and store an item's estimate from its latest applicable input."""
    item = connection.execute(
        "SELECT format, duration_seconds FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if item is None:
        raise LookupError(f"item {item_id} does not exist")

    item_format = ItemFormat(item[0])
    estimate = estimate_time_to_value_seconds(
        item_format,
        extracted_text=extracted_text,
        duration_seconds=item[1],
    )
    connection.execute(
        """
        UPDATE items
        SET time_to_value_seconds = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
        """,
        (estimate, item_id),
    )
    return estimate
