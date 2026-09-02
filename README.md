# modgud

A personal content triage system. Drop anything in — a URL, a video, a podcast,
a PDF — and get back a summary, the claims it makes, and for audio/video a
timestamped map of where the value actually is.

Named for Móðguðr, who guards the bridge and asks who you are before letting
you across.

## Status

Pre-implementation. See [DESIGN.md](DESIGN.md) for the settled design and
[milestone 1](DESIGN.md#build-order) for what gets built first.

## Development

Install the project and its development tools with uv:

```console
uv sync
```

The installed command exposes its available options through standard CLI help:

```console
uv run modgud --help
```

Run the same quality gate used by CI:

```console
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Storage

`BlobStore` keeps fetched bytes and UTF-8-encoded extracted text beneath its
root as `sha256/<first-two-hex>/<full-sha256>`. The full digest remains visible
in every filename, while the two-character directory keeps large stores easy
to browse. On each SQLite item, `content_hash` references the raw blob and the
nullable `extracted_text_hash` references extracted text when available.
`duration_seconds` retains audio/video metadata, while the nullable
`time_to_value_seconds` stores the digest-ready estimate. Text estimates use
the extracted text at 200 words per minute; audio/video estimates use duration,
and missing inputs leave the estimate unknown.

## Shape

- **In**: CLI, a LAN web drop box, email (Postmark inbound, polled).
- **Through**: format-specific extraction, then transcription and
  summarization against any OpenAI-compatible endpoint — local by default.
- **Out**: a morning digest that is complete on its own, plus a web UI for
  digging deeper.
- **Kept**: SQLite. Raw transcripts, summaries, and your own worth-it labels,
  in open formats that outlive any model choice.
