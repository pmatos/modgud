"""Behavioral tests for per-item time-to-value estimates."""

from pathlib import Path

import pytest

from modgud.database import connect
from modgud.formats import ItemFormat
from modgud.time_to_value import (
    estimate_time_to_value_seconds,
    recompute_time_to_value,
    time_to_value_sort_key,
)


def test_text_time_to_value_is_reading_time_at_200_words_per_minute() -> None:
    extracted_text = " ".join(["word"] * 250)

    estimate = estimate_time_to_value_seconds(
        ItemFormat.WEB,
        extracted_text=extracted_text,
    )

    assert estimate == 75


@pytest.mark.parametrize("item_format", [ItemFormat.PODCAST, ItemFormat.YOUTUBE])
def test_audio_and_video_time_to_value_uses_duration_metadata(
    item_format: ItemFormat,
) -> None:
    estimate = estimate_time_to_value_seconds(
        item_format,
        extracted_text=" ".join(["transcript"] * 1_000),
        duration_seconds=90.2,
    )

    assert estimate == 91


@pytest.mark.parametrize(
    ("item_format", "extracted_text"),
    [
        (ItemFormat.WEB, None),
        (ItemFormat.WEB, " \n\t"),
        (ItemFormat.YOUTUBE, "a transcript without duration metadata"),
    ],
)
def test_time_to_value_is_unknown_without_the_applicable_input(
    item_format: ItemFormat,
    extracted_text: str | None,
) -> None:
    estimate = estimate_time_to_value_seconds(
        item_format,
        extracted_text=extracted_text,
    )

    assert estimate is None


def test_unknown_time_to_value_sorts_after_known_estimates() -> None:
    estimates = [None, 90, 15, None, 45]

    ordered = sorted(estimates, key=time_to_value_sort_key)

    assert ordered == [15, 45, 90, None, None]


def test_time_to_value_is_recomputed_and_stored_when_extraction_lands(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url,
                content_hash,
                format,
                state,
                source,
                time_to_value_seconds
            ) VALUES ('https://example.com/article', 'content', 'web',
                      'captured', 'example.com', 999)
            """
        )
        item_id = item.lastrowid
        assert item_id is not None

        estimate = recompute_time_to_value(
            connection,
            item_id=item_id,
            extracted_text=" ".join(["word"] * 400),
        )
        stored = connection.execute(
            "SELECT time_to_value_seconds FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()[0]

    assert (estimate, stored) == (120, 120)


def test_time_to_value_is_recomputed_and_stored_from_duration_metadata(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "modgud.sqlite3") as connection:
        item = connection.execute(
            """
            INSERT INTO items (
                canonical_url,
                content_hash,
                format,
                state,
                source,
                duration_seconds
            ) VALUES ('https://www.youtube.com/watch?v=example', 'video',
                      'youtube', 'captured', 'Example Channel', 90.2)
            """
        )
        item_id = item.lastrowid
        assert item_id is not None

        estimate = recompute_time_to_value(
            connection,
            item_id=item_id,
        )
        stored = connection.execute(
            "SELECT time_to_value_seconds FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()[0]

    assert (estimate, stored) == (91, 91)
