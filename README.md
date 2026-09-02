# modgud

A personal content triage system. Drop anything in — a URL, a video, a podcast,
a PDF — and get back a summary, the claims it makes, and for audio/video a
timestamped map of where the value actually is.

Named for Móðguðr, who guards the bridge and asks who you are before letting
you across.

## Status

Pre-implementation. See [DESIGN.md](DESIGN.md) for the settled design and
[milestone 1](DESIGN.md#build-order) for what gets built first.

## Shape

- **In**: CLI, a LAN web drop box, email (Postmark inbound, polled).
- **Through**: format-specific extraction, then transcription and
  summarization against any OpenAI-compatible endpoint — local by default.
- **Out**: a morning digest that is complete on its own, plus a web UI for
  digging deeper.
- **Kept**: SQLite. Raw transcripts, summaries, and your own worth-it labels,
  in open formats that outlive any model choice.
