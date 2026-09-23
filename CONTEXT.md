# modgud — domain vocabulary

The words this codebase uses for its own concepts, so that modules, tests, and
commit messages name the same thing the same way. `DESIGN.md` explains *why* the
system is shaped as it is; this file fixes *what things are called*.

## Item

A single captured piece of content — an article, a YouTube video, a PDF, a
podcast episode. Identified by its canonical URL, stored once, and carrying
exactly one **state** at a time (`captured`, `extracted`, `summarized`,
`unsummarizable`, `failed`; see *Item lifecycle* in `DESIGN.md`).

## Capture

One run of taking a URL to a stored item: canonicalize, fetch, detect the
format, extract what text there is, and write the row. A capture that finds the
item already stored is still a capture — it records that the URL was seen again
rather than creating a second item.

## Reprocess

Re-running extraction for an existing item from the raw content stored at
capture, with no fetch. It exists for items that ended a capture without text —
a web page whose extraction failed, or a PDF captured before PDFs were
extracted. It is refused for an item that already has extracted text, because
that item's summaries are derived from it. The outcome is recorded with the
ordinary `extracted`, `unsummarizable` and `failed` event types; there is no
separate reprocess event.

## Event log

The append-only record of everything that has happened to an item, stored in the
`events` table. Every row is one **event**: an `item_id`, a **event type**, and
a JSON **payload** of fields describing it. Rows are never updated or deleted —
database triggers enforce that — so the log is the system's memory of how an
item reached its current state.

Ten event types exist: `captured`, `extracted`, `failed`, `caption_refused`,
`unsummarizable`, `summarized`, `audio_fallback`, `podcast_transcript`,
`digest_sent`, `label`.

The log is written through exactly one module, `modgud.events`, which owns the
table, the type vocabulary, and the payload encoding. Nothing else in the
package writes `INSERT INTO events`. Reading the log is deliberately *not* that
module's job: every reader (`origin_reports`, `digests`, `web`, `cli`, `reprocess`) queries
`events` joined to `items` with its own analytic SQL, and those queries share no
shape worth abstracting.

### Item log

An `ItemLog` is one item's slice of the event log, bound to one open
transaction. It is the interface callers use to append events. It never opens a
connection and never commits — the caller's `with connect(...)` block owns the
transaction, and the log writes inside it, so an item's row update and the
events describing it land or fail together.

## Payload encoding

Event payloads are JSON objects rendered with sorted keys, compact separators,
and non-ASCII escaped. The encoding is canonical — decided once in
`modgud.events` — so that payloads written by different call sites are
comparable, and so that a lone surrogate (which `surrogateescape`-decoded fetch
input can carry into an error message) can be recorded rather than raising at
the database binding.

An optional payload field that is absent is **omitted** from the object rather
than stored as `null`. The exception is a capture's `origin`, which is always
written, including as `null`: the origin report distinguishes "captured with no
known origin" from "origin not recorded" using `json_type`.

## Source texts

What an item's stored extracted text becomes when it is handed to a model. A
document format yields exactly one source text — the stored blob decoded as
UTF-8. A transcript format yields one per **transcript chunk**, in order. Every
summarization-shaped feature consumes this same shape, which is why tier-1
summaries, tier-2 long-form summaries and span maps can all be driven from one
loop over source texts.

Source texts are resolved through exactly one module, `modgud.source_material`,
which owns the lookup of an item's format, extracted text and chapters, the
document-versus-transcript decision, and the chunking. Nothing else in the
package derives source texts from an item id. What the module deliberately does
*not* own is **which formats each caller accepts**: that is the caller's policy,
passed in, because tier 1 and tier 2 genuinely differ — tier 2 excludes PDFs and
tier 1 does not.

## Transcript chunk

A slice of an item's transcript, carrying model-visible text and the exact
timing that text came from. Timings are held by code and never asked of a
model, so a span can't be hallucinated onto the wrong moment. There is one
chunking mechanism in the system, not two: every reader of an item's transcript
— span-map generation, tier-1 and tier-2 summarization, and the transcript page
— goes through `modgud.source_material`, so their chunk boundaries cannot drift
apart.

## Origin

Where a capture came from — the inbound mail target, the web drop box, or the
CLI. Carried on the `captured` event's payload and reported on by
`modgud.origin_reports`, which ranks origins by how much of what they send turns
out not to be worth it.

## Label

A reader's verdict on an item, `worth-it` or `not-worth-it`, recorded as a
`label` event from a signed one-click link in a digest. Labels are the system's
only feedback signal and are meant to outlive any model or implementation
choice.
