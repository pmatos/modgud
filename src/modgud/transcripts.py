"""Transcript chunking with timestamps kept outside model-visible text."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from html import unescape

from modgud.youtube import Chapter

_TIMING_LINE = re.compile(
    r"^(?:(?P<start_hours>\d{2,}):)?"
    r"(?P<start_minutes>\d{2}):(?P<start_seconds>\d{2})\."
    r"(?P<start_milliseconds>\d{3})\s+-->\s+"
    r"(?:(?P<end_hours>\d{2,}):)?"
    r"(?P<end_minutes>\d{2}):(?P<end_seconds>\d{2})\."
    r"(?P<end_milliseconds>\d{3})(?:\s+.*)?$"
)
_CUE_TAG = re.compile(r"<[^>\n]+>")


@dataclass(frozen=True)
class TranscriptChunk:
    """Model-visible text paired with exact structural timing."""

    id: str
    text: str
    start_ms: int
    end_ms: int


def format_timestamp(milliseconds: int) -> str:
    """Render a millisecond offset as an H:MM:SS or MM:SS clock reading."""
    total_seconds = milliseconds // 1_000
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def chunk_transcript(
    content: bytes,
    *,
    chapters: Iterable[Chapter] = (),
    max_chars: int = 6_000,
) -> tuple[TranscriptChunk, ...]:
    """Turn a WebVTT transcript into timestamp-free text with exact bounds."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")

    lines = content.decode("utf-8-sig").splitlines()
    cues: list[tuple[int, int, str]] = []
    line_index = 0
    while line_index < len(lines):
        timing = _TIMING_LINE.fullmatch(lines[line_index].strip())
        if timing is None:
            line_index += 1
            continue

        cue_lines: list[str] = []
        line_index += 1
        while line_index < len(lines) and lines[line_index].strip():
            line = unescape(_CUE_TAG.sub("", lines[line_index].strip()))
            if line:
                cue_lines.append(line)
            line_index += 1
        text = "\n".join(cue_lines)
        if text:
            cues.append(
                (
                    _timestamp_ms(timing, "start"),
                    _timestamp_ms(timing, "end"),
                    text,
                )
            )

    if not cues:
        return ()

    cues = [
        (start_ms, end_ms, part)
        for start_ms, end_ms, text in cues
        for part in _split_text(text, max_chars)
    ]
    transcript_digest = sha256(content).digest()
    chapter_starts_ms = tuple(
        sorted({round(chapter["start_time"] * 1_000) for chapter in chapters})
    )
    chunks: list[TranscriptChunk] = []
    pending: list[tuple[int, int, str]] = []
    for cue in cues:
        if pending and any(
            pending[0][0] < chapter_start <= cue[0]
            for chapter_start in chapter_starts_ms
        ):
            chunks.append(_to_chunk(pending, transcript_digest))
            pending = []
        while pending and _text_length((*pending, cue)) > max_chars:
            break_after = _natural_break_after(pending)
            chunks.append(_to_chunk(pending[:break_after], transcript_digest))
            pending = pending[break_after:]
        pending.append(cue)
    if pending:
        chunks.append(_to_chunk(pending, transcript_digest))
    return tuple(chunks)


def _timestamp_ms(timing: re.Match[str], prefix: str) -> int:
    hours = int(timing.group(f"{prefix}_hours") or 0)
    minutes = int(timing.group(f"{prefix}_minutes"))
    seconds = int(timing.group(f"{prefix}_seconds"))
    milliseconds = int(timing.group(f"{prefix}_milliseconds"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000 + milliseconds


def _text_length(cues: tuple[tuple[int, int, str], ...]) -> int:
    return sum(len(cue[2]) for cue in cues) + len(cues) - 1


def _split_text(text: str, max_chars: int) -> tuple[str, ...]:
    parts: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        natural_breaks = [
            index + 1
            for index, character in enumerate(window)
            if character in ".!?…"
            and (index + 1 == len(remaining) or remaining[index + 1].isspace())
        ]
        whitespace_breaks = [
            index for index, character in enumerate(window) if character.isspace()
        ]
        if natural_breaks:
            break_at = natural_breaks[-1]
        elif whitespace_breaks:
            break_at = whitespace_breaks[-1]
        else:
            break_at = max_chars
        parts.append(remaining[:break_at].strip())
        remaining = remaining[break_at:].strip()
    if remaining:
        parts.append(remaining)
    return tuple(parts)


def _natural_break_after(cues: list[tuple[int, int, str]]) -> int:
    for index in range(len(cues) - 1, -1, -1):
        if cues[index][2].rstrip().endswith((".", "!", "?", "…")):
            return index + 1
    return len(cues)


def _to_chunk(
    cues: list[tuple[int, int, str]], transcript_digest: bytes
) -> TranscriptChunk:
    text = "\n".join(cue[2] for cue in cues)
    chunk_digest = sha256(transcript_digest)
    chunk_digest.update(cues[0][0].to_bytes(8, "big"))
    chunk_digest.update(cues[-1][1].to_bytes(8, "big"))
    chunk_digest.update(text.encode())
    return TranscriptChunk(
        id=f"chunk-{chunk_digest.hexdigest()[:16]}",
        text=text,
        start_ms=cues[0][0],
        end_ms=cues[-1][1],
    )
