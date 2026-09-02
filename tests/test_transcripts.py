"""Behavioral tests for timestamped transcript chunking."""

from modgud.transcripts import chunk_transcript
from modgud.youtube import Chapter


def test_webvtt_timestamps_are_structural_not_part_of_chunk_text() -> None:
    transcript = b"""WEBVTT

00:00:01.001 --> 00:00:03.507
The first useful claim.

00:01:02.250 --> 00:01:05.009
The supporting evidence.
"""

    chunks = chunk_transcript(transcript)

    assert [(chunk.text, chunk.start_ms, chunk.end_ms) for chunk in chunks] == [
        (
            "The first useful claim.\nThe supporting evidence.",
            1_001,
            65_009,
        )
    ]


def test_bounded_chunks_prefer_completed_thoughts_over_the_latest_cue() -> None:
    transcript = b"""WEBVTT

00:00:00.000 --> 00:00:01.000
A premise

00:00:01.000 --> 00:00:02.000
continues.

00:00:02.000 --> 00:00:03.000
Evidence begins

00:00:03.000 --> 00:00:04.000
and keeps going
"""

    chunks = chunk_transcript(transcript, max_chars=40)

    assert [(chunk.text, chunk.start_ms, chunk.end_ms) for chunk in chunks] == [
        ("A premise\ncontinues.", 0, 2_000),
        ("Evidence begins\nand keeps going", 2_000, 4_000),
    ]
    assert all(len(chunk.text) <= 40 for chunk in chunks)


def test_chapter_starts_split_chunks_even_when_the_text_fits() -> None:
    transcript = b"""WEBVTT

00:00:00.000 --> 00:00:01.000
Opening context

00:00:01.000 --> 00:00:02.000
still develops

00:00:02.000 --> 00:00:03.000
The practical design

00:00:03.000 --> 00:00:04.000
continues from here
"""
    chapters: tuple[Chapter, ...] = (
        {"start_time": 0.0, "end_time": 2.0, "title": "Context"},
        {"start_time": 2.0, "end_time": 4.0, "title": "Design"},
    )

    chunks = chunk_transcript(transcript, chapters=chapters)

    assert [(chunk.text, chunk.start_ms, chunk.end_ms) for chunk in chunks] == [
        ("Opening context\nstill develops", 0, 2_000),
        ("The practical design\ncontinues from here", 2_000, 4_000),
    ]


def test_chunk_ids_are_stable_for_a_transcript_and_distinguish_transcripts() -> None:
    transcript = b"""WEBVTT

00:00:00.000 --> 00:00:01.000
Same position, original transcript.
"""
    changed_transcript = transcript.replace(b"original", b"changed")

    first_run = chunk_transcript(transcript)
    second_run = chunk_transcript(transcript)
    changed = chunk_transcript(changed_transcript)

    assert [chunk.id for chunk in first_run] == [chunk.id for chunk in second_run]
    assert first_run[0].id != changed[0].id


def test_a_single_long_cue_is_bounded_without_fabricating_finer_timing() -> None:
    transcript = b"""WEBVTT

00:00:01.123 --> 00:00:05.987
First complete thought. Second thought.
"""

    chunks = chunk_transcript(transcript, max_chars=24)

    assert [(chunk.text, chunk.start_ms, chunk.end_ms) for chunk in chunks] == [
        ("First complete thought.", 1_123, 5_987),
        ("Second thought.", 1_123, 5_987),
    ]
    assert all(len(chunk.text) <= 24 for chunk in chunks)


def test_webvtt_cue_markup_and_inline_timestamps_never_reach_chunk_text() -> None:
    transcript = b"""WEBVTT
Kind: captions

cue-1
00:00:01.001 --> 00:00:03.507 align:start position:0%
<c.colorE5E5E5>First &amp; <00:00:02.250>second</c>
"""

    chunks = chunk_transcript(transcript)

    assert chunks[0].text == "First & second"
    assert (chunks[0].start_ms, chunks[0].end_ms) == (1_001, 3_507)
